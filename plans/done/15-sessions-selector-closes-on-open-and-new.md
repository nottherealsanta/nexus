# Plan 15: The Sessions selector closes when a session is opened or `/new` starts one (Ratatui)

Status: implemented and verified offline, including full native restart against a temporary daemon. Source: root `TODO.md` item 8,
quoted unchanged:

> - when a session is selected from the sessions modal, the modal should close.  /new

## 1. Intent

The Sessions selector (the centred overlay opened by `Ctrl+O`, `Ctrl+B`, `▤`, `/sessions`) closes,
for real and persistently, when:

1. a session row is opened (click, double-click, Enter), and
2. a new session is started from it or while it is open (`/new`, `Ctrl+N`, `Ctrl+X N`, the
   selector's `n`/`Ctrl+N` / `session_new`).

### Non-goals

- The legacy full-page dashboard path (hosts that send no `sidebar.available`) keeps its behaviour.
- Projects tab behaviour already closes on open (`nexus/ui/ratatui/sidebar.py:352-373`); unchanged.
- Desktop paused; web deprecated.

## 2. Current state (checked 2026-10-25)

- The selector is open while the **persisted** preference `sidebar_open` is true
  (`nexus/ui/ratatui/sidebar.py:77-79`; saved in `~/.config/nexus/tui.json`,
  `nexus/ui/ratatui/preferences.py:16-19`). `close()` sets it false (`sidebar.py:122-126`).
- Opening a session row: Rust sends the operation `session_open`; Python
  `workflows.py:669-672` calls `switch_project` and **does not close the sidebar**. `switch` only
  hides the dashboard (`nexus/ui/ratatui/actions.py:386`).
- Rust papers over it with a client-local flag: on a `session_open` operation it sets
  `session_dismissed = true`, so `is_open` returns false while the host still says open
  (`rust/tui/src/sidebar.rs:173-174`, `:365-376`); a later `reveal` bump un-hides it (`:381-383`).
  The host never acknowledges a close, so `sidebar_open` stays true in `tui.json`: the selector
  comes back on the next start.
- `/new`: `ShellActions.command("/new")` → `switch(new_id)` (`actions.py:730-735`); the
  `session_new` operation → `close_dashboard()` + `/new` (`workflows.py:656-661`). Neither calls
  `sidebar.close()`, and Rust does not set `session_dismissed` for `session_new`, so **the
  selector stays open over the new session**. This matches the `/new` in the TODO line.
- `open_session` from the sidebar action path (`sidebar.py:311-314`, `op == "open_session"` at
  `:374-375`) does not close either.

## 3. Design

Make the host (Python bridge) the single owner of "closed", and drop the client-side workaround.

1. `workflows.py` `session_open`: after a **successful** `switch_project`, call
   `await self.shell.sidebar.close()` when `sidebar.enabled`. A failed switch keeps the selector
   and its error toast (the existing comment's intent, `workflows.py:670-671`).
2. `sidebar.py` `open_session` (`op == "open_session"`): same, close after success.
3. `/new` in `actions.py` and `session_new` in `workflows.py`: close the sidebar (if open) before
   switching, so the new session is not covered. Put this in one helper, e.g.
   `ShellActions.close_selector()`, used by both and by Manager auto-moves if Plan 10 wants it.
4. Rust: remove `session_dismissed` and its `reveal` handling once the host closes
   (`sidebar.rs:173-174`, `:365-395`), or keep it only as an instant hide that is cleared when the
   host's `open` turns false. Keep the instant hide: it avoids one frame of the selector over the
   new session while the switch runs. Also set it for `session_new`.
5. Keep `reveal` semantics for `/sessions` (re-open shows the selector even if it was hidden).

### Host, events, persistence

- No host command and no event: this is client UI state stored in the terminal preferences, which
  is the right place (it is not session state).
- `sidebar_open` now becomes false after an open; the next client start begins with the selector
  closed (today it would reopen).

## 4. Tests and acceptance

- Python (`tests/test_ratatui_sidebar.py`): `session_open` success → `sidebar_open` false and
  dashboard hidden; failure → still open with the error; `open_session` op closes; `/new` and
  `session_new` close; Projects behaviour unchanged.
- Rust (`sidebar.rs` tests, e.g. next to `host_reveal_shows_a_sidebar_hidden_after_a_session_open`
  at `:1630`): `session_new` hides immediately; a host snapshot with `open=false` clears local
  state; `/sessions` after that shows it again.
- PTY (`tests/test_ratatui_pty_sidebar.py`): open the selector, Enter on a row → selector gone;
  open, press `Ctrl+N` → selector gone; restart the client → selector closed.
- Acceptance:
  - [x] Opening a session from the selector closes it and it stays closed after restart.
  - [x] `/new` (any entry point) never leaves the selector over the new session.
  - [x] A failed open leaves the selector and shows why.

## 5. Risks and open questions

- Q1. Should Escape-free browsing ("peek" several sessions while keeping the selector open) be kept
  behind a modifier (e.g. `Shift+Enter` opens without closing)? Not planned unless asked.
- Risk: other callers rely on the selector staying open after a switch (e.g. the Manager's
  `_blocked()` treats an open sidebar as non-blocking, `nexus/ui/ratatui/manager.py:273-276`).
  Closing it is compatible; verify `tests/test_manager_show.py` still passes.
- Documentation: update `docs/ratatui-parity.md` "Left sidebar" (`:2223-2297`) to say opening a
  session or starting a new one closes the selector.

### Follow-up verification (2026-02-21)

The `/new` helper now closes the selector after successful switch/bootstrap,
not before attempting creation. A regression test checks that a failed switch
never calls `close_selector`. Focused new-session tests pass. The wider actions,
sidebar and workflows run reports 169 passed / 1 failed: the project-open test's
sidebar stub lacks `enabled`, which existing `close_selector` requires. Native
PTY acceptance remains unverified; this plan is not newly marked complete.

Follow-up: the project-open test stub now includes `enabled=True`, matching the
real sidebar interface. The project-open form no longer closes the selector a
second time after `switch_project` has already closed it. The actions/sidebar/
workflows/projection/responsiveness and native-sidebar run passed 266 tests and
failed two existing picker PTY expectations (old directory suffix and editing
without entering picker mode). Those picker failures remain open, not skipped.

Restart-persistence coverage now flushes the closed selector preference and loads
a fresh `Preferences` instance. Sidebar, Manager-show and native-sidebar PTY
verification: 101 passed. Manager non-blocking behavior remains covered by the
existing `test_manager_show.py` suite. Full new-session restart journey is still
not separately verified.

### Finisher restart acceptance

Added `test_new_session_closure_survives_a_fresh_preferences_instance`: opens selector,
issues `/new fresh`, verifies agent selection, flushes preferences, constructs a fresh
Preferences instance and verifies closed state. This and all 54 sidebar tests pass.
Manager suite was included in the 203 passing focused tests; manager remains non-blocking.

### Acceptance continuation

Fresh Preferences reload after `/new` and after open both pass. Added failure
regression: a rejected switch leaves selector and persisted open preference
unchanged and propagates the actionable error. Focused sidebar/native/Manager/
notice run: 112 passed. This verifies restart preference persistence, not a
manually restarted daemon/client journey; the latter remains unverified.

### Full native restart acceptance

`test_shared_daemon_pty.py::test_new_session_selector_stays_closed_after_full_native_restart`
passes: real temporary daemon/UDS, open selector, Ctrl+N creates/switches and closes,
quit native bridge, launch a second client against the same daemon/preferences,
assert selector absent and persisted sidebar_open false. This closes the previous
manual restart gap without credentials or touching the user's daemon.
