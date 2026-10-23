# 10. Focused delivery and acceptance

Status: implemented and verified. Simplified phases 04–10 were delivered in order,
with each phase committed and pushed separately. No release or version bump is
part of this work.

## Delivered behavior

- Changed turns are saved as owned-worktree commits or hidden checkpoints in
  shared checkouts. Unchanged turns create no save; bounded failures report skips.
- The native session modal offers No worktree, Ask every time, and Always
  worktree. The project preference persists; Ask resolves once per new session.
- Editing children default to isolated worktrees independently of the session
  choice, with an explicit boolean override. Successful saved work returns
  automatically; conflicts preserve the child and are reported.
- Saved-turn rows open bounded diffs. Session integration requires a fresh user
  confirmation and performs a local fast-forward without pushing.
- Explicit cleanup refuses dirty/ignored files, keeps the branch and authenticated
  record, and allows the session checkout to be recreated on its next turn.

## Validation — 2026-10-23

- **743 passed** in the final related suite: checkpoints/worktrees, per-session
  roots, integration/cleanup, subagent runner/security, configuration, native
  workflow/projection, views, docs and layering. The run took 560.44 seconds.
- This run includes **three actual native PTY checks**: the three-mode picker and
  Ask resumption, an owned write/commit followed by opening its saved-turn diff,
  and composer execution in an external session root. PTY/local socket access was
  enabled for these offline tests. The mode chooser text capture was inspected.
- **1 passed** in `tests/test_worktree_sessions_acceptance.py`: Ask choice →
  editing-child isolation → automatic return and parent save → host turn diff →
  confirmed integration → checkout removal with branch preserved → unchanged
  session reuse at the same saved HEAD.
- Offline Ruff and `git diff --check` passed. Earlier phase-specific validation is
  recorded in the corresponding ledgers.

The full offline suite was not run. One unrelated session-search assertion failed
in combined phase-08 host runs and passed in isolation; the prior commit's host
facade suite passed three times. It was left outside this work. Rust source was
unchanged; the existing built native binary was exercised by the PTY tests.

## Boundaries

No queues, merger agents, watchers, PR automation, new setup framework, config
matrix, forced cleanup, automatic session landing, or web/desktop expansion.
Existing size/time bounds, Git authentication and root authority remain in force.
