# 10. Testing and rollout

## 1. Rules for every phase

- One phase = one or more PRs with Conventional Commit subjects (`refactor:` for F0/F2, `feat:`
  otherwise). F1 flips user-visible daemon behavior and needs **explicit approval for a minor
  bump** before merging (AGENTS.md).
- Tests next to their peers (`tests/test_<area>_*.py`), offline, using temp Git repos created
  with `git init` (as `tests/test_ratatui_journeys.py` does for worktrees today) and
  `ScriptedProvider`. No network; PR mode uses a bare repo as the remote and a fake `gh`.
- `pytest -q` and `ruff check nexus tests` green; `tests/test_layering.py`,
  `tests/test_ui_layering.py`, `tests/test_docs.py` (module-map rows) green.
- Native PTY check for every TUI change; `docs/ratatui-parity.md` updated, marked verified only
  after the check ran.
- `docs/` updated in the same change; "not verified" where it is not.
- Minimum Git version: decide in F3 (recommended ≥ 2.35; checked at project load, feature
  disabled with the reason below it).

## 2. Phases

### F0 — Shared daemon prep (no behavior change)

Scope: shared-daemon plan phase 0 (env threaded through runtimes, no cwd reliance, namespaced
extension modules, log tagging) on `main` (file 01 §2).
Tests: static guards (no `os.environ`/`os.getcwd`/`Path.cwd` in runtime-reachable modules,
allow-listed files), env isolation with two runtimes in one process, module namespacing.
Exit: full suite green; per-workspace daemon unchanged.
Docs: `docs/host.md`, `docs/config.md` (env sources).

### F1 — One daemon, many projects; switching

Scope: `ProjectRegistry`/`ProjectHost`, project identity, protocol v4 (`Hello.focus`,
`ProjectList/Open/Focus/Unload`), routing by session, resource limits, lifecycle CLI, aliases
migration, Projects tab and quick switcher, web bound to one project per tab.
Tests: file 01 §11; `tests/test_host_daemon_projects.py`, `tests/test_project_identity.py`,
`tests/test_project_aliases.py`; PTY journey switching projects.
Exit: launching `nexus chat` in three different repos attaches to one daemon; all three appear
in the Projects tab; switching is instant; `nexus daemon status` lists them; turns in two
projects run concurrently within the limits; `NEXUS_DAEMON=per-workspace` still works.
Docs: file 01 §12; `docs/decisions.md` row replacing per-workspace daemons.

### F2 — Per-session root

Scope: file 02.
Tests: `tests/test_session_root*.py`.
Exit: suite green with all sessions `main`; a test-only external root works end to end.
Docs: file 02 §10.

### F3 — Session worktrees and turn commits

Scope: file 03 §§2–4 (not promotion), file 07 §1 (setup, trust), the setup strip, timeline
rows, `/worktree`, Settings page toggles, `SessionTurnDiff` for commits.
Tests: `tests/test_worktree_sessions_create.py`, `test_worktree_sessions_commits.py`,
`test_worktree_setup.py`, `test_view_worktree.py`, `test_config_worktrees.py`,
`test_worktree_git_safety.py`, PTY journey (toggle, base picker, per-turn diff).
Exit: three sessions edit the same file in parallel in three worktrees with no interference;
every changing turn has a commit with trailers; the main checkout is byte-identical before and
after.
Docs: `docs/decisions.md` replaces `:81`; `docs/sessions.md`, `docs/agents.md`,
`docs/tools.md`, `docs/config.md`, `docs/events-and-view.md`, `docs/security.md`,
`docs/ratatui-parity.md`, `docs/module-map.md`.

### F4 — Checkpoints for main-checkout sessions (parallel with F3)

Scope: file 04.
Tests: `tests/test_worktree_checkpoints.py` (byte-identical HEAD/index/stash/tree; concurrency;
attribution; retention), PTY per-turn diff for a main session.
Exit: every changing turn of a main-checkout session has a checkpoint and a diff; capture time
logged; auto-disable on slow repos works.
Docs: file 04 §9.

### F5 — Writer subagents in worktrees

Scope: file 05.
Tests: `tests/test_worktree_sessions_subagents.py`; peers `test_builtin_task.py`,
`test_agents_manager.py`, `test_subagent_continuation.py`.
Exit: an orchestrator fans out two `task` writers in parallel; both merge into the session
branch; a constructed conflict is resolved by the parent via `worktree_merge_child`.
Docs: file 05 §11.

### F6 — Merge queue and local landing

Scope: file 06 §§2–7, 9; cleanup on land (file 07 §2); recovery (file 07 §3, file 06 §11);
Land/Sync/Queue UI; `nexus land`, `nexus sync`.
Tests: `tests/test_worktree_sessions_land.py`, `test_merger_agent.py`,
`test_worktree_cleanup.py`, `test_worktree_recovery.py`; PTY journeys for Land and Sync.
Exit (the user's scenario): sessions A, B, C in three worktrees on `main`; A lands (squash);
B and C immediately show `behind` with overlap; B's next turn sees the note; B cannot land until
it syncs; B syncs (with one conflict resolved by the merger), lands; C syncs and lands. `main`
has three squash commits; no user branch changed without a confirmation token; crash injection
at each land step recovers.
Docs: file 06 §13; `docs/decisions.md` amended for landing.

### F7 — PR mode, promotion, cleanup polish

Scope: file 06 §8, file 03 §5, file 07 §2 (remaining triggers), `WorktreeCleanup`, legacy
removal plan.
Tests: `tests/test_worktree_sessions_pr.py`, `test_worktree_sessions_promote.py`, cleanup peers.
Exit: PR-mode project pushes and opens a PR via fake `gh` with confirmation; fetch-based
base-moved notices; a main-checkout session promoted mid-work continues in a worktree with its
changes carried over.
Docs: `docs/config.md`, `docs/security.md` (push), `docs/cli.md`.

## 3. Cross-phase test matrix

| Scenario | F3 | F4 | F5 | F6 | F7 |
| --- | --- | --- | --- | --- | --- |
| Non-git project: features disabled with reasons, nothing else changes | ✓ | ✓ | ✓ | ✓ | ✓ |
| Repo with no commits (unborn HEAD) | ✓ | ✓ | | ✓ | |
| Target branch not `main` (e.g. `develop`), detached HEAD in main checkout | ✓ | ✓ | | ✓ | ✓ |
| Repo with a submodule | ✓ | ✓ | | ✓ | |
| Large tree (generated 20 000 files fixture, marked `slow`) | ✓ | ✓ | | ✓ | |
| Files with spaces/unicode names, symlinks, executable bit, deletes, renames | ✓ | ✓ | ✓ | ✓ | |
| User-made linked worktree as session root (external) | | ✓ | | | ✓ |
| Daemon restart mid-turn, replay reproduces view | ✓ | ✓ | ✓ | ✓ | ✓ |
| Two installs (two daemons) on one project | | | | ✓ | |

## 4. Rollout

1. F0–F2 behind no flag (no behavior change except F1's daemon flip, which has
   `NEXUS_DAEMON=per-workspace` as escape hatch for one minor release).
2. F3–F7 ship with `[worktrees] sessions = false` by default; the setup strip still shows the
   toggle so users can opt in per session.
3. Dogfood in this repository (it is often dirty with in-progress work, a good test of D6),
   then flip the `sessions` default after feedback (a `docs/decisions.md` row).
4. Legacy subagent integrate flow removed one minor release after F6 if Doctor reports no
   legacy records in use.

## 5. Risks

| Risk | Mitigation |
| --- | --- |
| F1 is a large refactor of the daemon | follow the shared-daemon plan's phased approach; keep per-workspace mode for one release |
| Per-session root misses a code path that still uses the daemon workspace | static test: no `runtime.workspace` use in tool/Git/shell paths outside an allow-list; review every `self.workspace` in `runtime.py` (54 occurrences today) |
| Disk usage of many worktrees | `max_worktrees`, cleanup on land, disk column in `/worktrees` |
| Users surprised by `nexus/*` branches and `refs/nexus/*` | Settings and docs say so; cleanup deletes merged branches; one command to delete all refs |
| Merge conflicts the agent resolves badly | land always shows the exact review; checks before land; the user confirms |
| Large repos make `git add -A` per turn slow | timing logged per capture/commit; auto-disable checkpoints with a notice above 5 s; commits in worktrees are local to that worktree's index, so cost scales with the change, not the repo (*not verified* for repos with huge untracked trees) |
