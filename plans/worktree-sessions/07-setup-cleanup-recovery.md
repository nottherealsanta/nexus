# 07. Basic worktree lifecycle

Status: implemented for explicit cleanup and recovery of authenticated session worktrees.
Session mode selection remains in the earlier runtime behavior and this phase does not
introduce a separate setup framework.

## Existing behavior and proposed behavior

Keep existing setup/copy behavior as compatibility behavior; this plan does not add
a setup framework. The new proposal is limited to the session choice and basic
worktree lifecycle below.

Offer three modes: No worktree, Ask every time, and Always worktree. Remember the
project preference. Ask mode prompts once before the first turn of each new session and reuses that
choice on later turns; Always mode creates a worktree without prompting; No worktree
stays in the current checkout. Reuse a session's selected root on later turns. A failed create
must name the path and Git error and leave the session usable in its current root.

Users can list and remove Nexus-managed worktrees through an existing command or
simple session action. Removal requires an authenticated Nexus session-worktree record,
an idle owner session, the exact registered checkout path and branch, and a clean
checkout. The checkout is removed without force; its branch and active session record
remain so the existing session recreation path can restore it. Dirty, ignored, or
Git-operation state is reported and preserved. Worktrees owned by the user or another
tool are ineligible.

## Boundaries

- No new setup scripts, copy frameworks, submodule policies, automatic cleanup,
  background cleanup, or restore workflow.
- Keep creation, listing, and removal explicit and bounded.
- A missing worktree path is reported as already removed; the authenticated active
  record and branch remain available for recreation.
- Cleanup is explicit; there is no automatic or background removal.

## Acceptance

Only authenticated Nexus session worktrees are eligible for removal. A fresh
confirmation is bound to the checkout state; changes after preview invalidate it.
Successful removal keeps the branch and record, and later session setup can recreate
the checkout from that branch. Dirty or ignored files, active or queued work, and an
in-progress Git operation prevent removal. Git errors require manual Git repair; no
automatic recovery or cleanup is attempted.

## Docs

Update worktree/session documentation with the implemented lifecycle and recovery
limits. State which failures require manual Git repair.
