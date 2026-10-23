# 08. Show worktree choices and saved turns

Status: implemented and verified in the native terminal.

## Behavior

Use the existing host and native TUI patterns to provide the minimum flow:

- The session modal offers exactly No worktree / Ask every time / Always worktree.
  Ask mode prompts once before the first turn of each new session; reuse that choice
  on later turns. Remember the project preference.
- Show the session's worktree or current checkout and report creation errors.
- Label turns that were saved in Git and let the user inspect their diff.
- Show clear integration and cleanup results where those actions exist.

A saved-turn label should identify the commit or checkpoint and provide a bounded
diff. Errors should include a useful reason and the relevant path or Git operation.
Do not show raw internal JSON.

## Boundaries

Keep changes in the native TUI and existing host protocol conventions. Do not add
project switching, queue screens, new CLI command families, web UI parity, or new
background status events for this proposal.

## Acceptance

A user can answer the per-session creation prompt, see whether a changed turn was
saved, open its diff, and understand any failed operation. Verify the flow in the
native TUI before marking it complete; otherwise label it unverified.

## Docs

Update `docs/host.md`, `docs/ratatui-parity.md`, and applicable CLI/session docs only
for behavior that exists.

## Validation

427 native workflow/projection, view, documentation and layering tests passed.
The mode/host/shell checks passed (12 tests); checkpoint/runtime peers passed
(16 tests). Two actual native PTY checks passed with local socket/terminal access:
the three-mode picker and first-turn Ask resumption, and an owned write followed
by clicking its saved-turn row to open the diff. The chooser capture was inspected
at `artifacts/worktree-ask-pty.txt` in the phase worktree.

An unrelated session-search assertion failed in two combined host runs and passed
in isolation; the prior commit's full host facade suite passed three times. No
full-suite claim is made here.
