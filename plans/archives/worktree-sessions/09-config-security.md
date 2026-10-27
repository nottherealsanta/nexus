# 09. Minimal configuration and Git safety

Status: implemented; existing configuration keys and Git boundaries preserved.

## Configuration

The three native modes use one project machine preference, keeping the existing
`[worktrees].sessions` boolean compatible. Without a remembered choice, true means
Always and false means Ask in the native client. Unattended Ask callers keep the
current checkout unless they explicitly select a worktree. No schema matrix or
new setup page was added.

## Safety rules

- Every changed turn is saved in Git: as a worktree commit or a hidden checkpoint
  using the already implemented backend. No-change turns are skipped.
- Keep work isolated to the selected session's root. The independent subagent
  `worktree` parameter defaults on for editing children and accepts an override.
- Do not change user branches, push, or integrate a session without the user's
  explicit request for that operation.
- Bound file counts, sizes, and diff output. Explain skipped or failed saves.
- Preserve user files and unrelated worktree state during create, merge, and
  cleanup. Stop when safety cannot be established.
- Keep Git credentials with Git; do not read or return credential material.

## Acceptance and docs

Review each Git operation for its exact repository and branch. Verify refusal paths
for dirty or missing state that could overwrite user work. Update `docs/config.md`
and `docs/security.md` only when actual behavior or keys change, and distinguish
verified behavior from proposal.

## Validation

311 focused checkpoint, configuration, mode, runtime, security, documentation and
layering tests passed; all 7 checkpoint tests passed separately. Offline Ruff and
diff checks passed. The new real-Git regression proves an unreceipted edit is
saved with identical user HEAD/index, and an unchanged follow-up produces no save.
Eligible main/external turns now check their snapshot directly; unattributed
changes are reported as foreign rather than silently skipped. Existing size/time
limits and explicit skip reporting remain in place.
