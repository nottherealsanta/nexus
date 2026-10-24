# Worktree sessions: plan index

Status: implemented in sequence, revised 2026-10-23. Files 01–03 retain their
historical implementation records. The simplified 04–10 work is implemented;
10 records final acceptance results. Each phase is committed and pushed separately.

**Superseded in part (2026-10-24):** the worktree modes (No worktree / Ask every time /
Always worktree), the first-message question, the main-checkout checkpoints (04) and the
per-session ownership of a worktree are being replaced by
[agent-managed worktrees](../planning/agent-managed-worktrees.md): every session starts in the
main checkout and the agent creates, lists, moves between and merges worktrees itself. That
plan is in progress (the UI/host/config/view track is written, the runtime track is separate);
the descriptions below record what files 01–10 built and are not the current behaviour where
they mention modes or checkpoints.

The design is deliberately small: every changed turn is saved in Git; each
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
| [04 Checkpoints](04-checkpoints-for-main-checkout.md) | Save changed turns in the current checkout | Implemented; native review in 08 |
| [05 Subagent worktrees](05-subagent-worktrees.md) | Independent child isolation and saved-work integration | Implemented, focused tests |
| [06 Integration](06-merge-queue-and-landing.md) | Confirmed local fast-forward at user's request | Implemented; native controls in 08 |
| [07 Lifecycle](07-setup-cleanup-recovery.md) | Create/reuse/list, safe removal | Implemented |
| [08 TUI and host](08-tui-and-host-protocol.md) | Per-session choice, saved-turn labels, diffs, clear errors | Implemented, native PTY verified |
| [09 Config and safety](09-config-security.md) | Preserve config and basic Git boundaries | Implemented, focused tests |
| [10 Delivery](10-testing-and-rollout.md) | Focused acceptance and honest validation | Verified: 743 related tests + lifecycle regression |

## Boundaries

No merge queues, watchers, merger agents, PR automation, setup framework, config
matrix, automatic cleanup, or automatic changes to user branches. Use existing
host, UI, Git, and documentation patterns. Keep each plan focused and verify only
what has actually been implemented.
