# Worktree sessions: plan index

Status: **proposal**, agreed with the user in a planning conversation on 2026-10-22. No code yet.
`docs/` is the source of truth and the code wins over these files (see `AGENTS.md`). Each phase
lands with its tests, its `docs/` updates and, where marked, a `docs/decisions.md` entry.

Inputs: `plans/REFERENCE_PROJECT_RECOMMENDATIONS.md` (P1, per-turn checkpoints); the unmerged
shared-daemon plan (`plans/SHARED_DAEMON_PLAN.md` on branch `worktree-shared-daemon-plan`,
commit `f9ec89f`, checked out at `.claude/worktrees/shared-daemon-plan/`); a survey of Nexus's
current worktree code; a read of `references/t3code` (revision `96bae54`) and
`references/opencode` (`dev`). No reference code is copied; ideas only.

## 1. Goals

1. **Parallel work in one repository without interference.** Each root session can run in its
   own Git worktree on its own branch; writer subagents get their own worktrees too.
2. **Every change is traceable** to the turn, the agent or the subagent that made it: real
   commits on Nexus-owned branches in worktrees, hidden checkpoint refs in the main checkout.
3. **Landing is simple, ordered and agent-assisted.** A local merge queue lands one session at a
   time; when one lands, the others are told immediately what came in and must sync before they
   land. PR-based projects push and open a PR instead.
4. **One daemon for the whole machine.** The daemon is not bound to one project: it hosts many
   projects, each with many worktrees and sessions, and the user switches between them easily.
   Where `nexus` is launched from only picks the initial focus.
5. **Works in any project.** Nexus is a harness used in other people's repositories: non-git
   folders (worktree features off, stated), any target branch, monorepos and submodules, large
   repositories, PR-based teams, projects that need setup in a fresh checkout.

## 2. Decisions taken with the user

| # | Topic | Decision |
| --- | --- | --- |
| D1 | What runs in worktrees | Both root sessions **and** writer subagents. Two independent settings toggles. |
| D2 | Session toggle UX | Default from settings, **visible in the new-session modal**. The user can **opt out before the first turn**. A session can be **promoted** into a worktree later, by the agent (a tool) or the user (an action). |
| D3 | Which subagents | **Writers only, by role**: agent frontmatter `isolation: worktree`; read-only roles never. |
| D4 | Daemon model | **One machine-wide daemon, many projects**; a project's worktrees are session roots inside that project, not separate projects. Settings, permission grants and the sidebar are per project. |
| D5 | Where Nexus commits | **Only on `nexus/*` branches and `refs/nexus/*` refs it created.** User branches change only at an approved land. |
| D6 | Dirty main checkout | **Start from `HEAD`** (or a chosen base) and warn "N uncommitted changes are not included". No refusal. |
| D7 | Who commits | **Both**: the harness commits leftovers at turn end; the agent may commit mid-turn via a tool. Trailers `Nexus-Session`, `Nexus-Turn-Seq`, `Nexus-Agent`. |
| D8 | Subagent landing | **Auto-merge** the subagent branch into the session branch on finish; conflicts go back to the parent agent. |
| D9 | Shell for worktree children | **Yes**, cwd pinned to the child worktree. |
| D10 | Opted-out sessions | **Hidden checkpoint refs** per turn, `refs/nexus/checkpoints/<session>/<seq>`, never touching HEAD, index or branches. |
| D11 | Land mode | Per project: `local` (default) or `pr`. |
| D12 | Sync | **Merge the target into the session branch**; turn commits keep their SHAs. |
| D13 | Base-moved notice | **Notice now, sync at land**: immediate event, context note at the next turn, sync required before land. |
| D14 | Target history | **Squash** by default; per-project `merge` or `ff`. The session branch is kept as a ref. |
| D15 | Plan shape | This directory of detailed files; the shared daemon is the foundation phase. |

### Recorded decisions this reverses

- `docs/decisions.md:81` ("Worktree integration applies frozen bytes to a clean parent with a
  rollback journal; no Git commit/reset/stash/checkout"). Replaced by D5 and file 06. What stays:
  an exact review before a user branch changes, single-use confirmation tokens, crash recovery,
  authenticated records. Replacement entry lands in phase F3 and is amended in F6.
- "One daemon per workspace owns the runtime" (`docs/host.md`, `docs/decisions.md`). Replaced in
  F0/F1 (file 01).
- `docs/decisions.md:263` ("Cross-project sessions without shared hosting"): opening a foreign
  session no longer reconnects to another daemon; it switches project inside one daemon.

## 3. Files and reading order

| File | Contents |
| --- | --- |
| [01-shared-daemon.md](01-shared-daemon.md) | One daemon, many projects; project registry and identity; switching; migration. Foundation. |
| [02-per-session-root.md](02-per-session-root.md) | A session's working root separate from its project; what resolves against which. |
| [03-session-worktrees-and-turn-commits.md](03-session-worktrees-and-turn-commits.md) | Creating a session worktree, per-turn commits, the agent commit tool, promotion. |
| [04-checkpoints-for-main-checkout.md](04-checkpoints-for-main-checkout.md) | Hidden checkpoint refs and per-turn diffs for sessions in the main checkout. |
| [05-subagent-worktrees.md](05-subagent-worktrees.md) | Writer subagents, `isolation`, shell, auto-merge into the session branch. |
| [06-merge-queue-and-landing.md](06-merge-queue-and-landing.md) | Queue per (project, target), sync, base-moved notices, `merger` agent, local and PR landing. |
| [07-setup-cleanup-recovery.md](07-setup-cleanup-recovery.md) | Worktree setup step, cleanup policy, crash recovery. |
| [08-tui-and-host-protocol.md](08-tui-and-host-protocol.md) | Every new host command with fields; native TUI screens and flows. |
| [09-config-security.md](09-config-security.md) | Config keys, scopes, defaults; trust and security rules. |
| [10-testing-and-rollout.md](10-testing-and-rollout.md) | Phases, exit criteria, test files, docs per phase, release notes. |

Read 01 → 02 first (foundation), then 03 → 06 (the feature), then 07–10.

## 4. Phase map

| Phase | Name | Files | Depends on | User-visible |
| --- | --- | --- | --- | --- |
| F0 | Shared daemon prep (env, cwd, module namespacing) | 01 | — | no |
| F1 | One daemon, many projects; project switching | 01, 08 | F0 | yes (minor bump, needs approval) |
| F2 | Per-session root | 02 | F1 | no |
| F3 | Session worktrees, turn commits, setup | 03, 07, 08 | F2 | yes |
| F4 | Checkpoints for main-checkout sessions | 04 | F2 | yes |
| F5 | Writer subagents in worktrees | 05 | F3 | yes |
| F6 | Merge queue, sync, base-moved, local land | 06 | F3 | yes |
| F7 | PR mode, promotion, cleanup polish | 03, 06, 07 | F6 | yes |

F3 and F4 can run in parallel after F2. Details and exit criteria: file 10.

## 5. How this compares to t3code (short)

t3code (read, not run) creates a worktree per thread at first send, chosen in the composer and
locked afterwards; checkpoints are hidden refs built from a temporary index; file restore only
in an unshared worktree; landing is commit → push → PR with model-written messages, merge on the
forge; ordering is left to forge merge queues; the only "base moved" signal is a PR watcher that
polls every 2 minutes. Nexus adopts the lazy creation, setup script, submodule policy,
checkpoint technique, restore-only-when-isolated rule and managed-only cleanup, and differs by
(1) a local merge queue as the default, (2) pushed base-moved notices with the incoming file list
and overlap, (3) real commits on session branches, (4) one daemon across projects with in-app
switching. Full comparison in file 06 §1.

## 6. Global open questions

Collected from the files; each file lists its own with a recommended default.

1. Release vehicle: F1 flips "one daemon per workspace", a user-visible change that needs
   explicit approval for a minor bump (AGENTS.md). Ask before merging F1.
2. Whether to revive `feat/shared-daemon` (2 commits from 2026-10-02, diverged from `main`) or
   re-implement F0 on `main`. Recommended: re-implement on `main`, using those commits as
   reference only (file 01 §9).
3. Windows: worktrees, sockets and path lengths are not verified. Recommended: unsupported in
   v1, stated.
