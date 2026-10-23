# Worktree sessions: plan index

Status: active proposal, revised 2026-10-23. Files 01–03 have already been pushed
and record their historical implementation. The backend checkpoint work in 04 is
implemented in commit `19dc207`; its ledger records unfinished UI work and retention-based cleanup.
File 05 implements independent child isolation and automatic return of saved work.
File 06 adds confirmed local fast-forward integration; native controls remain in 08.
File 07 adds explicit safe checkout removal and recreation.
Files 08–10 remain proposals until their implementation records say otherwise.
This plan index does not declare unfinished work complete.

The proposal is deliberately small: every changed turn is saved in Git; each
session chooses No worktree, Ask every time, or Always worktree; every subagent
that may edit defaults to its own worktree, with an independent per-call override. The user can inspect saved turns. User
branches change only after the user requests integration.

## User-confirmed direction

- Worktree modes: No worktree, Ask every time, and Always worktree. Remember the
  project preference. Ask mode prompts once before the first turn of each new
  session and reuses that choice on later turns.
- Subagents have an independent `worktree` parameter: omitted defaults on for
  editing children and off for read-only children; an explicit boolean overrides it. Successful child work integrates
  automatically; conflicts stop and are reported.
- Implement the plans sequentially, completing and reviewing each focused step.
- Successful isolated children return changes automatically. In a shared checkout,
  apply only their diff to working files without moving HEAD or the index.

## Plans

| File | Scope | Status |
| --- | --- | --- |
| [01 Shared daemon](01-shared-daemon.md) | Historical foundation plan and implementation ledger | Existing, pushed |
| [02 Per-session root](02-per-session-root.md) | Session working root | Existing, pushed |
| [03 Session worktrees and turn commits](03-session-worktrees-and-turn-commits.md) | Worktree sessions and per-turn commits | Existing, pushed |
| [04 Checkpoints](04-checkpoints-for-main-checkout.md) | Save changed turns in the current checkout | Backend implemented; TUI incomplete |
| [05 Subagent worktrees](05-subagent-worktrees.md) | Independent child isolation and saved-work integration | Implemented, focused tests |
| [06 Integration](06-merge-queue-and-landing.md) | Confirmed local fast-forward at user's request | Backend implemented |
| [07 Lifecycle](07-setup-cleanup-recovery.md) | Create/reuse/list, safe removal | Backend implemented |
| [08 TUI and host](08-tui-and-host-protocol.md) | Per-session choice, saved-turn labels, diffs, clear errors | Proposed |
| [09 Config and safety](09-config-security.md) | Preserve config and basic Git boundaries | Proposed |
| [10 Delivery](10-testing-and-rollout.md) | Focused acceptance and honest validation | Proposed |

## Boundaries

No merge queues, watchers, merger agents, PR automation, setup framework, config
matrix, automatic cleanup, or automatic changes to user branches. Use existing
host, UI, Git, and documentation patterns. Keep each plan focused and verify only
what has actually been implemented.
