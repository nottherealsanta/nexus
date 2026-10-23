# 04. Save changed turns in the current checkout

Status: backend checkpoint capture and host diff/restore contracts are implemented
(commit `19dc207`, per the existing ledger). Native diff presentation remains incomplete. Ref cleanup is retention-based; eager
purge cleanup is not required. The existing restore API need not be extended or used
by this proposal.

## Proposed behavior

Every turn that changes files is saved in Git and can be reviewed by turn. A session
in the main or an external checkout uses the existing hidden checkpoint mechanism;
a session in its own Nexus worktree uses the per-turn commit mechanism from 03.
No-change turns create no save.

Checkpoint capture must preserve the user's `HEAD`, index, branches, stash, and
working tree. It must use bounded Git operations and clearly report skipped saves
and capture errors. A shared checkout has a diff view, but no file restore action. The existing host
preview/confirm restore API remains unchanged and is outside this proposal.

## Existing implementation ledger — 2026-10-23

The backend captures checkpoints, replays events, retains bounded refs, and exposes
host diff and preview/confirm restore contracts. Capture runs during turn
preparation/terminal callbacks for main and external roots, including ordinary
sessions. The first baseline is captured eagerly to avoid missing shell edits.

- [x] Private temporary index, hidden refs, size guard, no-op detection, unborn
  HEAD handling, and bounded retention.
- [x] Durable capture/skip events, path attribution, bounded view projection,
  duration and HEAD-moved metadata, and shared-checkout restore refusals.
- [x] Real-Git coverage for preservation, untracked files, no-op turns, limits,
  concurrency, attribution, and HEAD movement.
- [x] Host diff and preview/confirm restore contracts with refusal checks.
- Checkpoint refs are not eagerly removed on session purge; bounded retention
  manages them. This is an existing limitation, not a new requirement.
- [ ] Show per-turn diffs in the native TUI. This has not been verified.

## Acceptance

A changed turn in a main-checkout session has a saved checkpoint and reviewable
turn diff. A no-change turn does not create a save. Capture leaves checkout state
untouched and reports bounded failures honestly. Native presentation is complete
only after a focused manual TUI review.

## Docs

Update the relevant session, event, security, and native TUI docs with the shipped
behavior. Mark anything not yet verified as such.
