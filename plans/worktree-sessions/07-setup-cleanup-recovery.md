# 07. Worktree setup, cleanup and recovery

Phases: setup in **F3**, cleanup on land in **F6**, the rest in **F7**. Recovery pieces land
with the feature they recover.

## 1. Setup of a new worktree

A fresh worktree has only tracked files: no `node_modules`, no virtualenv, no `.env`, no build
cache. Nexus runs in other people's projects, so this must be configurable per project.

### 1.1 Settings (project `.agents/nexus.toml`, `[worktrees]`)

| Key | Default | Meaning |
| --- | --- | --- |
| `setup` | none | argv list run in the new worktree, e.g. `["uv", "sync"]` or `["npm", "ci"]` |
| `setup_shell` | `false` | run `setup` through the user's shell instead (string form) |
| `setup_timeout` | `300` | seconds, max 1800 |
| `setup_on_fail` | `"fail"` | `fail` the first turn, or `warn` and continue |
| `setup_for_subagents` | `"copy_only"` | `full`, `copy_only`, `none` (file 05 §4.1) |
| `copy` | `[]` | untracked files/globs copied from the main checkout (`.env`, `.env.local`, `config/local.yml`) |
| `copy_max_mb` | `20` | total cap for copied files |
| `share_dirs` | `[]` | directories symlinked from the main checkout instead of rebuilt (`node_modules`); opt-in, documented as unsafe if the worktree changes dependencies |
| `submodules` | `"none"` | `none`, `top` (`git submodule update --init`), `recursive` |

### 1.2 Algorithm (file 03 §3.2 step 6–7)

1. Submodules per policy (bounded timeout 300 s; failures follow `setup_on_fail`).
2. Copy: resolve each pattern against the main checkout, **only untracked or ignored files**
   (tracked files are already there), refuse symlinks and paths escaping the checkout (same
   checks as `PathGuard`), stop at `copy_max_mb`; list copied files in the event.
3. Share: for each `share_dirs` entry that exists in the main checkout and not in the worktree,
   create a symlink; listed in the event and in the worktree status as "shared from main".
4. Setup command: run with cwd = worktree, the project's captured env (file 01 §4) plus
   `NEXUS_WORKTREE=1`, `NEXUS_PROJECT_ROOT`, `NEXUS_WORKSPACE` (= worktree); takes a process
   slot; output streamed into the session as a labelled step (like a shell job), tail recorded.
5. Append `worktree.setup_ran {command, exit, duration_ms, copied (≤ 50), shared, submodules,
   output_tail ≤ 8 KiB, reason: "created"|"recreated"}`.

### 1.3 Trust

`setup` is code execution taken from the repository. Project command hooks are already treated
as trusted code (`docs/extensions.md:10`, `:120-131`), so this is not a new class of risk, but
it runs **automatically** on a common action (opening a session). Rule: the first time a given
`setup` value (by sha256 of argv/string) runs in a project, the TUI asks once ("This project
runs `npm ci` in each new worktree. Allow?"; Allow always for this project / Allow once /
Skip). The answer is stored per project in machine state
(`~/.nexus/projects/<key>/trust.json`), not in the repository. `nexus run` (non-interactive)
follows the unattended policy (`tools/permissions.py`): skip with a warning unless pre-approved.
User-scope `setup` (in `~/.nexus/`) needs no prompt. Reconcile with `docs/security.md` in F3
(*not verified* whether hooks are prompted today; they appear not to be).

## 2. Cleanup

### 2.1 Policy

| Trigger | Default | Action |
| --- | --- | --- |
| Session landed (local) | `remove_on_land = true` | remove the worktree dir if clean; keep the branch and `refs/nexus/landed/...` |
| PR merged (PR mode) | detected on fetch when the target contains the PR head or the PR is merged per `gh pr view` | same as above |
| Session archived | keep | nothing |
| Session deleted (trash) | keep | nothing (restore must work) |
| Session purged | `remove_on_purge = true` | remove worktree and branch **if** landed or has no commits beyond base; otherwise keep and list as orphaned |
| Subagent merged | always | remove the child worktree; keep the child branch until the session lands, then delete it |
| Idle worktrees | off | `remove_idle_days = 0` (off); when set, remove clean worktree dirs of sessions with no activity for N days (branch kept; recreated on next turn) |

### 2.2 Rules

- Only remove worktrees whose service record says Nexus owns them (HMAC-verified), never a path
  merely under the service root, never an external worktree.
- Never remove a dirty worktree automatically; list it ("3 uncommitted changes") with a manual
  "Commit and remove" / "Discard and remove" (confirmed).
- Never delete a branch that has commits not contained in any user branch or landed ref, except
  on an explicit discard confirmed by typing the session name.
- Removal: `git worktree remove <path>` (non-forced), then record `lifecycle`. A failure is
  logged and retried at the next cleanup pass (`cleanup_pending`, existing state).
- Recreate on demand: a later turn in a session whose worktree dir was removed runs file 03
  §3.3.

### 2.3 Cleanup pass

Runs at project load, after each land, and hourly while loaded (bounded: ≤ 50 removals per pass,
each Git call 60 s). Doctor and `/worktrees` show: active, removable, orphaned, dirty, and disk
usage (`du` bounded to 10 s per worktree, else "unknown").

## 3. Recovery after a crash

At project load (in `ProjectHost`, before accepting turns for that project), under the service
lock, extending `recover_pending` / `recover_transactions` (`nexus/agents/worktrees.py:1588`):

| Found | Action |
| --- | --- |
| Record `pending` with no `worktree.created` in the session log | remove the worktree and branch it allocated; record `removed` |
| Record `pending` with `worktree.created` in the log | promote to `active` (the event is the truth) |
| `MERGE_HEAD` in a session worktree with no matching `synced`/`subagent_merged`/`sync_conflict` event | a sync or child merge was interrupted: if conflicts exist, append `sync_conflict` (state `conflicts`); if none, `git merge --abort` and re-run the merge on next request |
| Land journal present | file 06 §11 |
| Temp checkpoint index files `nexus-checkpoint-index-*` older than 10 min | delete |
| Lock files with dead holders | flock semantics release them; nothing to do |
| Record `active` whose directory is gone | leave; recreated on next turn |
| Branch of an `active` record missing | mark record `detached`; the session's next turn shows the labelled error (file 02 §6) |

Recovery never touches the main checkout's working tree or index.

## 4. Failure modes

| Failure | Behavior |
| --- | --- |
| Setup prompts for input | stdin is closed; it fails or times out; output tail shown |
| Setup writes outside the worktree | not prevented (it is trusted code); documented |
| Copy pattern matches a huge file | stops at `copy_max_mb`, lists skipped files |
| User deletes `.nexus-worktrees-*` by hand | records gone: worktrees become unknown to Nexus; `git worktree prune` is suggested by Doctor; session logs still name branches, so "Recreate from branch" works |
| Disk full during `worktree add` | file 03 §6 |

## 5. Tests

- `tests/test_worktree_setup.py`: argv run with env vars; timeout; `fail` vs `warn`; copy of
  untracked/ignored only, symlink and escape refusal, size cap; share_dirs symlinks; submodule
  policies against a temp repo with a submodule; trust prompt once per digest (fake approval
  service); non-interactive skip.
- `tests/test_worktree_cleanup.py`: each trigger; dirty worktree kept; external never removed;
  branch kept vs deleted rules; recreate after removal; pass bounds.
- `tests/test_worktree_recovery.py`: every row of §3 by constructing the state on disk.

## 6. Docs

`docs/config.md` (`[worktrees]` setup and cleanup keys), `docs/security.md` (setup trust),
`docs/agents.md` (service records v3, lifecycle), `docs/sessions.md` (what delete/purge does to
worktrees).

## 7. Open questions

1. Trust prompt for project `setup` (recommended) vs treat like hooks (no prompt).
2. `remove_on_land` default `true` (recommended; branch kept, recreated if needed) vs keep.
3. Worktree location: keep the sibling service root (recommended; reuses records and recovery)
   vs `~/.nexus/worktrees/<project>/` (t3code uses its home dir). The sibling root can surprise
   users who `ls ..`; document it, and offer `[worktrees] dir` (user scope) later.
