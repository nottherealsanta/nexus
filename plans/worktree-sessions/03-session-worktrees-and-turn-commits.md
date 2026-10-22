# 03. Session worktrees and per-turn commits

Phases: **F3** (create, commit, agent tool, events) and **F7** (promotion). Depends on F2.

## Implementation ledger — 2026-10-23

The session-worktree backend and promotion are implemented. Protocol **v6** adds
session selection, status, promotion, detach and digest-bound setup trust actions;
no compatibility shim is maintained. Validation and remaining boundaries are recorded below.

- [x] Lazy first-turn allocation from a selected local branch, offline friendly-name
  slugs with a six-hex suffix, dirty-main warning counts, non-Git refusal, live
  worktree limits, unsupported-layout refusal and exact failure cleanup.
- [x] Separate authenticated v3 session records in the existing locked service root;
  pending intent precedes Git creation. Epoch timestamps advance with lifecycle
  changes. Existing subagent records keep their own review lifecycle.
- [x] Accepted prompts are durable before provisioning. Context, AGENTS and tools
  freeze after the owned root exists; preparation runs outside the event loop.
  Creation failures keep drafts editable. Stop kills setup and records cancellation.
- [x] Missing owned directories recreate on the existing branch and rerun setup;
  missing branches fail with a labelled error. Pending creation and promotion
  records recover without touching the original checkout.
- [x] Harness commits before the terminal event on completed, failed or cancelled
  changed turns; no commit for unchanged turns. Session/turn/agent trailers,
  deterministic subjects, bounded file summaries, identity fallback, size limits,
  conflict and branch guards, and non-fatal commit warnings are implemented.
- [x] `worktree_commit`, `worktree_status` and approval-required `worktree_promote`;
  selected commits preserve unrelated staged files and use literal pathspecs.
  Shell commits are observed with bounded file/count metadata. Durable turn-head
  events recover commit observation after an interrupted turn.
- [x] Setup argv/shell mode, output/tail limits, timeout, fail/warn policy,
  submodule policy, bounded untracked-file copy and explicit shared-directory
  symlinks. Native permission UI asks about untrusted project setup; unattended
  setup skips unless approved. One-time grants belong to their session; project
  trust is stored in machine state and bound to the reviewed digest.
- [x] Promotion carries successful session edit-tool writes only. Before-image
  blob IDs and after-image hashes reject earlier foreign changes, intervening
  writers and later edits. The original checkout and its index remain unchanged;
  excluded and remaining files are listed. Carry commits, continuation origin and
  interrupted promotion intents are durable and retryable.
- [x] Owned forks allocate a distinct branch at their first turn, based on their
  parent's current committed tip; composer/tools cannot borrow the parent's
  owned root before then. Detach keeps the owned checkout and branch.
- [x] Bounded event/view projections, last 200 commits and authoritative turn-ID
  links; docs, module map and real-Git/native regression coverage.

Resolved implementation decisions:

- Commits use an **isolated temporary index, `commit-tree` and compare-and-swap
  `update-ref`** rather than porcelain `git commit`. This prevents an unrelated
  pre-staged file from entering a selected commit and prevents a racing shell
  ref move from advancing a user branch. Hooks and signing are disabled. The
  normal owned index is refreshed only against the committed object; failures
  leave an explicit warning. Main/external indexes are never used for commits.
- Before-image reads are capped at 50 MiB; unknown or unstable ownership refuses
  carry-over. Promotion is capped at 500 files and 128 MiB total. File receipts
  store hashes and session/call identity, never file bytes. Forks do not inherit
  ownership of another session's main-checkout edits.
- Git subprocesses have a 60-second deadline and a streaming 1 MiB output cap.
  Dirty-main counts use a separate streaming 10,000-entry cap, so a large dirty
  checkout produces a clipped warning instead of refusing worktree creation.
- Git observation and status are bounded snapshots. Target watching, sync, queue
  position and landing are file 06. Writer-subagent isolation/auto-merge is file 05;
  automatic removal policies are file 07. External roots remain user-owned.
- The new-session toggle/base picker, session action menus and expanded timeline
  controls are the dedicated native UI work in file 08; this phase supplies their
  host contract and existing native tool/permission rendering. Non-Git status
  returns the disabled reason. No deprecated browser feature UI was added.
- No automatic restore of promoted files is offered: the notice leaves the
  original bytes for the user. A separate confirmed restore action belongs to
  the later session-action UI. Checkpoint refs/diffs are file 04; before those
  exist, promotion conservatively proves a continuous edit chain from HEAD.

Verification (2026-10-23):

- Full offline suite: **8,180 passed, 364 skipped, 6 deselected, 17 failed** in
  24m18s. Twelve failures are the native rendering failures reproduced on the
  unchanged baseline in files 01/02. One unchanged Claude-worker cancellation
  test cancelled before its startup file was written; its complete file passed
  **48 tests** on rerun. Four event/replay regressions from an unnecessary
  selection-lock event were fixed by using the existing durable `turn.started`
  lock instead. The full suite was not rerun after these fixes.
- Final acceptance: **45 passed**, covering config, host commands, views, setup
  trust and both native session-root/worktree PTY checks. Real Chromium project
  navigation also passed.
- Service creation/commit checks: **21 passed** after bounded status/history
  fixes; final commit checks **11 passed**, including the configured size-limit
  upper bound. Runtime/promotion/tool checks: **25 passed**. Receipt/file/patch
  checks previously passed **137 tests**, and legacy child worktree regression
  checks passed **66 tests**.
- Final event/replay/worktree rerun: **118 passed**, including all four full-suite
  regressions, golden view replay, event catalog, runtime, promotion and security.
- Ruff and `git diff --check` pass. This is not a green full-suite claim; the
  baseline rendering failures and timing failure remain visible above.

## 1. Contract

- With `[worktrees] sessions = true` (or the toggle on in the new-session modal), a new root
  session in a Git project runs in its **own worktree on its own branch** `nexus/session/<slug>`,
  created from a base branch at its first turn.
- Every turn that changed files ends with **at least one commit** on that branch. Commits carry
  trailers identifying the session, turn and agent. The agent may also commit mid-turn.
- Nexus commits **only** on branches it created (D5). The main checkout and the user's branches
  are never written by this file's code.
- In a non-git project the toggle is shown disabled with "Worktrees need a Git repository".

## 2. Data model

### 2.1 Service record (extends `nexus/agents/worktrees.py`)

Reuse the service root (`<project parent>/.nexus-worktrees-<name>/`, `nexus/runtime.py:3292`),
its HMAC-authenticated records, `.lock` and recovery. Record format v3 adds:

```
kind: "session" | "subagent"          # v2 records are "subagent"
session_id: str                       # owner session (root session for both kinds)
parent_record: str = ""               # subagent: the session worktree it branches from
base_ref: str                         # target branch name (refs/heads/...), session only
base_sha: str
branch: str                           # nexus/session/<slug> | nexus/agent/<session-slug>/<child>
path: str                             # absolute, daemon-only
lifecycle: "pending" | "active" | "landing" | "landed" | "detached" | "removed" | "cleanup_pending"
created_at, updated_at: float
```

The existing subagent lifecycle (`active → finalized → integrated|cleanup_pending|discarded`,
`docs/agents.md:696`) stays for legacy v2 records (file 05 §7).

### 2.2 Names

| Thing | Format |
| --- | --- |
| Session branch | `nexus/session/<slug>`; slug = sanitized session name (Nexus already assigns adjective-color-animal names, `docs/host.md:603`) + `-` + 6 hex; ≤ 64 chars |
| Worktree dir | `<service root>/worktrees/<slug>` |
| Subagent branch | `nexus/agent/<session-slug>/<child-id>` (file 05). Not under `nexus/session/<slug>/…` because Git cannot hold both `a/b` and `a/b/c` as refs. |
| Legacy | `nexus/subagent/<slug>` (`worktrees.py:46`) |

Using the session name rather than a model-generated name keeps creation deterministic and
offline; t3code's model rename is not adopted (§9 Q1).

### 2.3 Events (session log; bounded; documented in `docs/events-and-view.md`)

| Event | Payload |
| --- | --- |
| `session.root_set` | file 02 §3 |
| `worktree.created` | `branch`, `base_ref`, `base_sha`, `dirty_main: {modified, untracked}` (counts), `path_display` |
| `worktree.setup_ran` | file 07 |
| `worktree.committed` | `sha`, `seq` (turn seq), `by: "harness"|"agent"|"subagent"`, `agent_id`, `subject` (≤ 200 chars), `files` (≤ 200 paths, `clipped` count), `insertions`, `deletions` |
| `worktree.commit_skipped` | `reason: "no_changes"|"too_large"|"git_error"|"conflict_in_progress"`, `detail` (redacted, ≤ 500 chars) |
| `worktree.detached` | session left its worktree (opt-out after creation is not allowed; see §4.3) |

View (`ConversationView.worktree`): branch, base, ahead/behind (filled by file 06), last 200
commits (sha, seq, by, subject, counts), uncommitted counts, state. Each turn row in the
timeline links to its commits (`seq` → commits).

## 3. Creating a session worktree

### 3.1 New-session modal (file 08 §3.1)

Fields: `Worktree [on/off]` (default `[worktrees] sessions`), `Base` (default
`[worktrees] base`, which defaults to the main checkout's current branch; a picker lists local
branches, bounded to 200), and when the main checkout is dirty: "12 uncommitted changes in the
main checkout are not included (based on `main` @ `a1b2c3d`)". The toggle stays editable until
the first prompt is sent (D2). `SessionWorktreeSet` changes it before the first turn.

### 3.2 Lazy creation at first turn

Runs in the `ProjectHost` after the first prompt is persisted and before the turn's model call,
under the service-root lock, with Git through `worktrees._git` (controlled env,
`worktrees.py:274-320`):

1. Refuse with a labelled turn error if: not a Git project; `max_worktrees` reached (file 09);
   the base ref does not resolve. The main checkout's state (dirty, mid-merge) is **not** a
   reason to refuse (D6).
2. `base_sha = git rev-parse --verify <base_ref>^{commit}`.
3. Count dirty state of the main checkout for the warning only:
   `git status --porcelain=v2 -z` bounded to 10 000 entries (count, "10 000+" when clipped).
4. Allocate slug and path; write record `lifecycle = pending`.
5. `git worktree add -b nexus/session/<slug> <path> <base_sha>`. Use `--no-checkout` +
   `git checkout` only if sparse-checkout is configured in the main checkout (*not verified*;
   §9 Q4).
6. Submodules per `[worktrees] submodules` (file 07 §1).
7. Run setup (file 07 §1); its output streams into the turn as a labelled step.
8. Record `lifecycle = active`; append `worktree.created` and `session.root_set`.
9. Continue the turn with the new root.

Any failure in 4–7 removes exactly what this call allocated (as `_cleanup_created` does,
`worktrees.py:898`) and fails the turn with the reason; the session stays a draft with the
toggle editable, so the user can retry or switch it off.

### 3.3 Opening an existing session

If the record is `active` but the directory is gone, recreate it from the branch
(`git worktree prune`, then `git worktree add <path> nexus/session/<slug>`), re-run setup, and
append a `worktree.setup_ran` with `reason = "recreated"` (file 07 §2).

## 4. Per-turn commits

### 4.1 Harness commit at turn end

Runs after the turn's last tool result is persisted and before `turn.ended` is emitted, so the
commit is part of the turn. Skipped for `main` and `external` roots (they get checkpoints,
file 04).

1. If a merge, rebase, cherry-pick or revert is in progress in the worktree (`MERGE_HEAD`,
   `rebase-merge/`, … in the worktree's gitdir): emit `worktree.commit_skipped
   (conflict_in_progress)`; the next sync/land will report it (file 06).
2. `git add -A` in the worktree (honours `.gitignore`; excludes nested worktrees and the service
   root because they are outside the worktree).
3. If `git diff --cached --quiet`: emit nothing (no change) and stop.
4. Size guard: if staged changes exceed `[worktrees] commit_max_files` (default 2 000) or any
   file > `commit_max_file_mb` (default 50), unstage, emit `worktree.commit_skipped (too_large)`
   with the counts, and show a labelled warning; the changes stay uncommitted in the worktree.
5. Subject: `nexus: <first line of the turn's user prompt, ≤ 72 chars>` (deterministic, no model
   call). Body: the files list (≤ 50 lines, "and N more"). Trailers:

   ```
   Nexus-Session: <session name> (<logical id>)
   Nexus-Turn-Seq: <seq>
   Nexus-Agent: root
   ```

6. `git commit --no-verify --no-gpg-sign -F -` with the user's Git identity
   (`user.name`/`user.email` from the repo config; if unset, `Nexus <nexus@localhost>` and a
   one-time warning). `--no-verify`: project hooks do not run on harness commits (§9 Q2);
   signing off because a signing prompt would hang the daemon.
7. Emit `worktree.committed {by: "harness", …}`.

All Git calls are bounded (timeout 60 s each, output caps as `worktrees._git`). A Git failure
emits `worktree.commit_skipped (git_error)` and never fails the turn.

### 4.2 Agent commit tool

`worktree_commit(message: str, paths: list[str] | None = None)` (builtin, mutating, approval
rules apply; only offered to sessions whose root is a worktree):

- `message` ≤ 4 000 chars; trailers are appended by Nexus (the agent cannot forge them; any
  `Nexus-*:` lines in the message are stripped and noted in the result).
- `paths` (≤ 500) are checked by `PathGuard`; omitted = all changes.
- Same steps as §4.1 (2, 4, 6) with `Nexus-Agent: <agent id>`; emits `worktree.committed
  {by: "agent"}`. The result row shows sha, subject, counts and files.

The agent may also run `git commit` through the shell (normal shell permissions; project hooks
run then). The turn-end harness commit picks up anything left. Commits made through the shell
on the session branch are detected at turn end by comparing `HEAD` to the last recorded sha and
recorded as `worktree.committed {by: "agent", via: "shell"}` (≤ 50 per turn, else a count).

### 4.3 Guards on the branch

- Before every harness/agent commit, check `HEAD` is `refs/heads/nexus/session/<slug>`. If the
  agent switched branches through the shell, refuse to commit and emit a labelled warning ("the
  worktree is on `<x>`, expected `<branch>`"); `worktree_status` reports it.
- The agent's shell may run any Git command in its worktree (normal permissions). Nexus does not
  try to stop `git reset` or `git rebase` there; the session branch is Nexus-owned and the
  events record what Nexus committed. Rewritten history is detected at land (file 06 §5 step 2).
- **No opting out after creation.** Once the worktree exists, "switch off" means **Detach**:
  the session continues in the main checkout and the worktree stays until landed or discarded
  (`worktree.detached`). Recommended to hide Detach behind the session actions menu.

### 4.4 `worktree_status` tool and view

`worktree_status()` returns: branch, base ref and sha, target tip, ahead/behind counts,
uncommitted counts (modified, added, deleted, untracked, conflicted), last 5 commits, pending
base-moved notices (file 06), queue position if queued. Read-only, always allowed.

## 5. Promotion (F7)

Moving a running session from the main checkout (or an external root) into a new worktree.

Triggers: the user action "Move to worktree" (`SessionPromote`), or the agent tool
`worktree_promote(reason: str, continuation: str = "")` which needs approval (it changes where
the session works).

Algorithm (at a turn boundary; the agent tool ends the turn, like t3code's handoff):

1. Determine the session's own changes: files touched by this session's successful edit tools
   since its last checkpoint (file 04) intersected with `git status` of the root. Files changed
   by others (other sessions, the user) are excluded and listed.
2. If any of those files also changed outside this session since the last checkpoint (content
   differs from what the session last wrote), refuse: "can't separate this session's changes in
   `<files>`; commit or move them yourself". Bounded to 500 files.
3. Create the worktree from `HEAD` of the main checkout (not the base branch tip), as §3.2.
4. Apply the session's changes into the worktree (copy file bytes, or delete) and commit as
   `nexus: carry over changes from the main checkout` with trailers.
5. The main checkout is **left as is**: Nexus does not revert the files there (D5). The result
   lists them: "these files are still modified in the main checkout: …; revert them when you're
   ready" with a one-click `git restore` offered as a separate confirmed action (*decision*,
   §9 Q3).
6. Append `session.root_set (worktree)`; if `continuation` is set, queue it as the next prompt
   with `origin = "promote"`.

## 6. Failure modes

| Failure | Behavior |
| --- | --- |
| `git worktree add` fails (path exists, branch exists, disk full) | turn fails with Git's message (redacted, ≤ 500 chars); draft stays editable; nothing left behind |
| Setup fails (non-zero) | turn fails by default; `[worktrees] setup_on_fail = "warn"` continues with a warning |
| Commit fails | `commit_skipped (git_error)`; changes stay in the worktree; next turn retries |
| Branch deleted by the user | next turn: labelled error, offer "Recreate branch from last recorded commit" (sha from events) or Detach |
| Daemon crash between `worktree add` and `worktree.created` | record is `pending`; recovery removes the worktree and branch if the session log has no `worktree.created` (file 07 §3) |
| Two sessions pick the same base and edit the same files | no interference; conflict surfaces at land (file 06) |
| Repo uses `core.worktree`, sparse checkout or partial clone | *not verified*; detect and refuse with "unsupported repository layout" in v1 |

## 7. Tests

- `tests/test_worktree_sessions_create.py`: lazy creation at first turn; toggle flip before
  first turn creates nothing; base branch selection; dirty-main counts; non-git refusal; failure
  cleanup leaves no branch/dir/record; recreation of a missing dir; `max_worktrees` refusal.
- `tests/test_worktree_sessions_commits.py`: harness commit per changed turn, none for unchanged;
  trailers; subject from prompt; size guard; `--no-verify` (a failing pre-commit hook in the
  fixture repo does not block); identity fallback; branch-switched guard; shell commits detected;
  `worktree_commit` tool with paths and forged trailer stripping.
- `tests/test_worktree_sessions_promote.py` (F7): carry-over, refusal on foreign edits, main
  checkout untouched, continuation queued.
- Reducer/replay: `tests/test_view_worktree.py`.
- All against real `git init` repos in temp dirs (as `tests/test_ratatui_journeys.py` does
  today), no network.

## 8. Docs

`docs/agents.md` (worktree service now also for sessions), `docs/sessions.md` (session
worktrees, lifecycle), `docs/tools.md` (`worktree_commit`, `worktree_status`,
`worktree_promote`), `docs/events-and-view.md`, `docs/decisions.md` (replace `:81`, see
README §2), `docs/config.md`.

## 9. Open questions

1. **Branch names**: session name + hex (recommended) vs model-generated (t3code).
2. **Project Git hooks on harness commits**: skipped with `--no-verify` (recommended; hooks run
   at land time instead, file 06 §5) vs run them (slow, may prompt, may fail every turn).
3. **Reverting promoted files in the main checkout**: offer a separate confirmed `git restore`
   of exactly those files (recommended) vs never touch.
4. **Sparse checkout / partial clone**: refuse in v1 (recommended) vs mirror the sparse config.
5. **Commit message model**: deterministic subject (recommended) vs a cheap-tier model per turn
   (costs a request per turn; could be opt-in later).
