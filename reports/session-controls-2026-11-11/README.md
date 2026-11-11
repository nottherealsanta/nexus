# Session controls — plans 29–32 (2026-11-11)

## Scope and decisions

Both desktop clients share the same behavior. Mobile keeps the compact Sessions/Manager/Settings client.

- Archive events close tabs for every archived tree member, regardless of source; `/archive` exposes the existing Core operation. `/search [text]` opens focused search (desktop switcher; mobile Sessions list).
- Exactly one question skips review: single choice and free text submit immediately, multi-select has one submit. Multiple questions retain step-through/review. Questions lock and preserve composer drafts; approvals stay unchanged.
- **User clarification:** only queued rows have edit/up/down/remove icons, with no Steer button. Existing keyboard actions remain. Icons use same-group bounds.
- Worktree icon has a “Worktree session” hint and opens a small modal with full copyable worktree path, branch and main-checkout path; no Manage link.

## Shared inputs

- `interface/manifest.toml`: `/archive`, `session.worktree_info`, `session.linked_worktree` availability.
- `interface/theme.toml`: `control.worktree` (`⑂`, ASCII `*`).
- `interface/scenarios/session-controls.json`: question, preserved draft, pending inputs and linked/main worktree data for both clients.

## Verification

- Shared manifest: passed (213 commands, 38 slash names).
- Shared theme with NSH overlay: passed (602 checks).
- Focused unchanged Core contracts: `tests/test_archive.py` and `tests/test_inputs_integration.py` — **28 passed**, isolated scratch `NEXUS10_HOME`, no provider calls.
- Core archive/input contracts: `tests/test_archive.py` + `tests/test_inputs_integration.py` — **28 passed**.
- TUI `cargo test --test session_controls` — **34 passed**; matching question/queue/worktree screenshots captured.
- NSH `npm run build` — passed. Full web session-controls run: **12 passed, 4 failed**. Archive/search, question flows,
  failed-answer retry, archive refusal, and the basic worktree modal/copy passed. Three pending-row tests fail because
  the rows are not visible; the long-path test fails its scroll assertion. A subsequent isolated queue rerun still fails
  to display the rows. Basic NSH question/worktree screenshots are present; the queue capture is a diagnostic
  empty-state screenshot, not a parity screenshot.
- Paired screenshot review, NSH pending-row fix, NSH long-path scroll verification, and phone-route/device checks remain
  open. This report is a partial checkpoint, not a completion claim.
