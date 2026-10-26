# Plan 10: Manager screen, `Ctrl+X .`, GPT Live opens it, composer talks to it, auto-move to launched sessions (Ratatui)

Status: proposed (2026-10-25). Nothing in this plan is implemented. Source: root `TODO.md`
items 1 and 2, quoted unchanged:

> - auto move to new session while talking to manager
>
> - Manager your screen needs to look different - open with ctrl + x and "." . this also opens when gpt-live is initiated. but manager can be talked to by composer itself as well.

The two items are planned together because "auto move" decides what happens *on* the new
manager screen when the manager launches a session.

## 1. Intent

1. The Manager gets its own screen, visibly different from an ordinary session conversation.
2. `Ctrl+X .` opens (and closes) it.
3. Starting a GPT Live call opens it.
4. The composer on that screen sends typed messages to the manager, also while a Live call runs.
5. When the manager launches a session while the user is talking to it (typing or Live), the
   client moves to that new session.

### Non-goals

- Desktop (GPUI) is paused (`AGENTS.md:8`); web is to be deprecated (`AGENTS.md:109`). Native Ratatui only.
- No change to the manager's backend rules: tools, provenance, caps, pinned model, consent
  (`docs/manager.md`). No new manager tool.
- No change to `[manager] enabled = false` as the default.

## 2. Current state (checked 2026-10-25)

| Fact | Where |
| --- | --- |
| The manager is "a view, not a tab": the ordinary conversation layout with the left title and workspace row blanked, composer label `Manager · <model>` | `docs/manager.md:65-72`, `docs/ratatui-parity.md:1703-1723` |
| Toggle is `Ctrl+X A` (either case) → `action("manager_toggle")` | `rust/tui/src/main.rs:1481-1500` |
| Leader table row `("a", "toggle_manager", …)`; `.` is not bound in Rust or in the table | `nexus/ui_support/shortcuts.py:35-56`, `rust/tui/src/main.rs:1461-1530` |
| `Manager.toggle` / `open` / `close` remember and restore `(workspace, session)` | `nexus/ui/ratatui/manager.py:86-132` |
| GPT Live always attaches to the manager and **never switches the screen** (documented decision) | `nexus/ui/ratatui/live.py:198-228`, `docs/ratatui-parity.md:1730-1744` |
| The composer already sends to the manager when the manager is the session in front | `nexus/ui/ratatui/actions.py:549-623` (`target = self.controller.session`) |
| **Launch autofocus already exists**: a fresh `session.linked` (`kind: "managed"`) switches to the child when the manager is in front on this client or this client's Live call follows it | `nexus/ui/ratatui/manager.py:219-256`, called from `nexus/ui/ratatui/prototype.py:1534-1540` and `nexus/ui/ratatui/manager_follow.py:75-76`; documented `docs/manager.md:154-158`, `docs/ratatui-parity.md:961-964` |
| Autofocus is "not verified" in a live PTY; there is no PTY test of `Ctrl+X A` | `docs/ratatui-parity.md:964`, `:1772` |

So TODO item 1 is **already implemented in code but unverified**. Known gaps (read from the code,
not observed live):

- G1. Only the first launch of a turn can move the user: after it, the manager is no longer in
  front, so `mine` is false at `manager.py:240` and later launches are dropped silently (unless a
  Live call follows the manager).
- G2. When a guard blocks the move (`_blocked`, `manager.py:265-281`) the user gets a warning toast
  with no action to go there later.
- G3. No end-to-end test drives a `session.linked` through `prototype.update` with a real binary.

## 3. Assumptions (must be confirmed by the user; marked so implementers do not guess)

- A1. **"look different"**: the user gave no mockup. This plan proposes a concrete layout (§4.1).
  If the user has a picture in mind, it replaces §4.1, not the rest.
- A2. **`Ctrl+X .`** is read as: press the leader, then the period key. It is added *next to*
  `Ctrl+X A`, which stays (removing it would break the documented, tested binding
  `tests/test_ratatui_manager.py:236-237`). Ask the user whether `A` should be retired.
- A3. **"also opens when gpt-live is initiated"** reverses the documented "Live never switches the
  session on screen" decision. This plan makes the reversal explicit and records it.
- A4. **"manager can be talked to by composer itself as well"** is read as: on the Manager screen
  the composer is a first-class input next to voice, and stays usable during a Live call. It is
  **not** read as "type to the manager from any other session" (that would need a routing prefix and
  a new rule for which session a draft goes to). That broader reading is open question Q2.
- A5. **"auto move … while talking to manager"** is read as: the manager is in front on this client
  (typing) or this client's Live call follows it (voice), which is the existing targeting rule.

## 4. Design

### 4.1 Manager screen layout (proposal for A1)

Drawn when `manager.active` is true (snapshot field already exists, `prototype.py:1322`). Same
regions as a conversation (`render::regions`, `rust/tui/src/render.rs:1798`), different content:

```
┌ ◆ Manager · codex/gpt-6-luna · Live: listening ●  ───────────────── ▣ ▤ ▥ ┐  top bar: no tabs
│ Managed sessions (3)                                                      │  board: up to 6 rows
│  ● fix-flaky-sync     working  2m   ⚑ needs you                           │
│  ✓ release-0-7-1      done     14m                                        │
│  ○ docs-pass          idle     1h                                         │
├───────────────────────────────────────────────────────────────────────────┤
│ (manager conversation transcript, ordinary blocks)                        │
├───────────────────────────────────────────────────────────────────────────┤
│ Talk to the Manager…                                       [Live ● 0:42]  │  composer
└───────────────────────────────────────────────────────────────────────────┘
```

- **Top bar**: no session tabs (the manager is not a tab); a left label `◆ Manager · <model>` and
  the Live state chip when a call is open. Right buttons unchanged (`◆` pressed).
- **Board** (new, collapsible with a click on its heading): one row per managed root, newest
  first, from data the client already polls: the managed ids from `ManagerState`
  (`docs/manager.md:52-56`) joined with `SessionDashboard` rows (title, status glyph, attention).
  Click/Enter on a row = `switch_project` to it (the ordinary switch; `Ctrl+X .` returns).
  Bounded: 6 rows plus `+N more · /sessions`. No new host command is needed; if the join turns out
  to need data the dashboard does not carry, add a host field rather than reading storage (rule 4,
  `AGENTS.md:97-100`).
- **Composer**: placeholder `Talk to the Manager…`; while a Live call is open it stays enabled and a
  typed message goes to the manager like any message (the host accepts `SessionEnqueue` on the
  manager; Live delegations also arrive as queued user input, `docs/manager.md:170-176`).
- **Live pane**: on this screen the Live pane is drawn expanded by default (it already exists,
  `rust/tui/src/live_bar.rs`), so voice and text share one screen.
- Accent: the existing manager accent; `NEXUS_ASCII=1` fallbacks as in `render/chrome.rs`.

All of this is presentation over existing durable state; nothing new is persisted.

### 4.2 `Ctrl+X .`

- Rust: in the leader branch (`main.rs:1496`) accept `KeyCode::Char('.')` alongside `a`/`A` →
  `action("manager_toggle", "")`.
- Python: add a `LEADER_SHORTCUTS` row `(".", "toggle_manager", "Open or close the Manager")`.
  Keep the `a` row (A2). `tests/test_ratatui_actions.py:369-410` and
  `tests/test_ratatui_settings_simple_pages.py:60-68` count rows; update their expectations.
- `tests/test_desktop_keymap_parity.py:87` asserts every leader row has a desktop route. Desktop is
  paused, so either add the trivial keymap row in `rust/desktop/src/keymap.rs` **only if the user
  allows a desktop touch**, or mark the row native-only in the parity test. Decide before coding
  (Q4).

### 4.3 GPT Live opens the Manager screen

- In `Live.start` (`nexus/ui/ratatui/live.py:198`), after the call connects (`_connect` succeeds),
  call `shell.manager.open()` unless the manager is already in front. Open **after** connect so a
  refused consent or an unavailable helper never moves the user.
- Respect `Manager._blocked()` (`manager.py:265`): if a form, panel, recording or pending prompt is
  open, do not switch; show a toast with an action `Open Manager` (toast actions exist,
  `docs/ratatui-parity.md:1338-1356`).
- `Manager.previous` records the session the user was in, so `Ctrl+X .` returns there.
- Ending the call does not switch back (the user may be mid-conversation with the manager).
- Update the documented decision: `docs/ratatui-parity.md:1730-1734` ("never switch the session on
  screen") and `docs/manager.md` ("Native presentation"); add an entry to `docs/decisions.md`
  (what changed and why: the user wants the manager visible during a call).

### 4.4 Auto-move to launched sessions (close G1–G3)

- Keep the existing guards (replay, 30 s, dedupe, `_blocked`).
- G1: when a launch arrives and this client already auto-moved for the **same manager turn**
  (`event.turn`), do not move again; raise an info toast `Manager launched <title>` with an `Open`
  action (operation `{"kind": "session_open", "workspace", "id"}`, already allowed by the
  dashboard path, `workflows.py:669-672`). Remember `(manager session, turn)` in a bounded set.
  Rationale: moving the user twice in one breath is disorienting; the first move is what the user
  asked for.
- G2: the blocked toast (`manager.py:250-251`) gets the same `Open` action.
- Q1 decides whether, on the new Manager screen, the first launch should still move the user away
  (current behaviour) or only highlight the new row on the board. Default in this plan: **keep
  moving** (it is what the TODO asks for).

## 5. Implementation steps (order)

1. §4.2 shortcut (smallest, independent).
2. §4.4 G1/G2 in `nexus/ui/ratatui/manager.py` + tests.
3. §4.3 Live opens the screen in `nexus/ui/ratatui/live.py` + doc decision update.
4. §4.1 screen layout: Python projection of the board (new module, e.g.
   `nexus/ui/ratatui/manager_board.py`, with a `docs/module-map.md` row) feeding a snapshot field
   `manager_board`; Rust drawing in `rust/tui/src/render/` (new `manager.rs` or inside `chrome.rs`)
   with one shared layout function for drawing and hit-testing (the pattern in
   `render/chrome.rs` `agent_layout`/`top_layout`).

## 6. Tests and acceptance

- Python: `tests/test_ratatui_manager.py`: leader `.` row present; `Live.start` success opens the
  manager and records `previous`; blocked → toast with `Open`; consent refused → no switch.
  `tests/test_manager_show.py`: two `session.linked` in one manager turn → one switch, one toast.
- Rust: `main.rs`/input test: `Ctrl+X .` emits `manager_toggle`; render tests for the board (rows,
  `+N more`, narrow widths, ASCII mode), hit-test parity with drawing.
- PTY: extend `tests/test_ratatui_pty_manager.py` with `Ctrl+X .` open/close (needs the debug
  binary, `docs/manager.md:392-394`).
- Acceptance:
  - [ ] `Ctrl+X .` toggles the Manager screen; `Ctrl+X A` still does.
  - [ ] The Manager screen is visibly distinct (board + label) and its composer sends to the manager, including during a Live call.
  - [ ] Starting Live opens the Manager screen unless something modal is open (then a toast with `Open`).
  - [ ] A manager launch moves the user once per manager turn; later launches toast with `Open`.
  - [ ] Docs updated: `docs/manager.md`, `docs/ratatui-parity.md`, `docs/decisions.md`; screenshots reviewed or "not visually verified" stated.

## 7. Risks and open questions

- Q1. On the new screen, should a launch still move the user away from the manager? (default: yes)
- Q2. Should the composer of *another* session be able to address the manager (e.g. a prefix)? Not
  planned; needs a routing rule and must not change where an ordinary draft goes.
- Q3. Exact visual design (A1); the board is a proposal.
- Q4. Desktop keymap parity test vs. paused desktop work.
- Risk: Live opening a screen can interrupt someone mid-draft in another session. Drafts are kept
  per `composer_key` (`prototype.py:1350`), so text is not lost; verify in the PTY test.
- Risk: the board's join of `ManagerState` with dashboard rows can go stale; poll cadence is
  already 30 s / 2 s (`docs/ratatui-parity.md:1700-1702`, `:2242`). State staleness is acceptable;
  label it with the age.
- Not verified anywhere yet against real Codex or GPT Live (`docs/manager.md:397`).
