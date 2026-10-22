# 04. Checkpoints for sessions in the main checkout

Phase: **F4**. Depends on F2. Independent of F3.

## 1. Contract

- Sessions whose root is the main checkout or an external worktree (file 02 §5) get a **hidden
  checkpoint** at the end of every turn that changed files: a commit object reachable from
  `refs/nexus/checkpoints/<session-id>/<seq>`.
- Taking a checkpoint **never** changes `HEAD`, the index, any branch, any file in the working
  tree, or the stash (D10).
- The user can see a **per-turn diff** for every turn, labelled and bounded, on the same screen
  as worktree sessions' per-turn commits (file 08 §3.5).
- **No file restore in a shared checkout** (t3code's rule). Restore exists only for worktree
  sessions, as a revert commit on the session branch (§6).
- Also used by file 03 §5 (promotion) to find a session's own changes.

Source of the technique: t3code `apps/server/src/vcs/GitVcsDriver.ts:830-1170` (read, not
copied).

## 2. Data model

Refs: `refs/nexus/checkpoints/<session-id>/<seq>` where `<seq>` is the turn's `turn.started`
seq, zero-padded to 10 digits so refs sort. A baseline `…/<seq>-base` is taken at the start of
the first changing turn of a session (§3 step 0).

Event `checkpoint.captured`:

```
seq: int                     # turn seq
ref: str                     # full ref name
commit: str                  # checkpoint commit sha
head: str                    # HEAD sha at capture (parent of the checkpoint commit)
base_ref: str                # the previous checkpoint ref (or -base)
files: list[str]             # ≤ 200; paths changed vs base_ref, filtered (§4)
files_clipped: int
foreign: int                 # files changed vs base_ref that this session did not touch
insertions: int, deletions: int
```

`checkpoint.skipped {seq, reason: "not_git"|"too_large"|"git_error"|"disabled", detail}`.

View: `ConversationView.checkpoints` (last 200) and per-turn link `seq → checkpoint`.

## 3. Capture algorithm

At turn end (same hook point as file 03 §4.1), for `main`/`external` roots, if
`[worktrees] checkpoints = true` (default true in Git projects):

0. If this session has no checkpoint yet, first capture a **baseline** the same way at
   `turn.started` of this turn (the turn-start hook records it before the first tool runs), so
   the first turn's diff is real. Baselines are skipped for turns that run no mutating tool.
1. Skip with `checkpoint.skipped (no_changes)` if the turn ran no successful mutating tool
   **and** no shell job (cheap pre-check; a shell job may write anything).
2. Let `GIT_DIR` = the root's gitdir, `common` = `git rev-parse --git-common-dir`.
3. Temporary index: copy `<gitdir>/index` (if present) to
   `<common>/nexus-checkpoint-index-<uuid>`; set `GIT_INDEX_FILE` to it for steps 4–5 only.
4. `git add -A -- .` against the temporary index (honours `.gitignore`; untracked files
   included; bounded by `[worktrees] commit_max_files` and `commit_max_file_mb`, otherwise
   `checkpoint.skipped (too_large)`).
5. `tree = git write-tree`.
6. `commit = git commit-tree <tree> -p HEAD -m "nexus checkpoint <session> <seq>"` with author
   and committer `Nexus <nexus@localhost>` (a fixed identity: these objects are never pushed and
   never land).
7. `git update-ref refs/nexus/checkpoints/<session>/<seq> <commit> ""` (create-only).
8. Delete the temporary index in a `finally`.
9. Compute the diff summary vs the previous checkpoint (`git diff --numstat -z <prev> <commit>`,
   bounded) and emit `checkpoint.captured`.

Concurrency: two sessions in the same checkout may capture at once; each uses its own
temporary index, and refs are per session, so they do not collide. Git's object writes are
safe concurrently. `git add` against a temp index takes `<common>/nexus-checkpoint-index-*.lock`,
not the real `index.lock`, so it does not block the user's Git commands (*not verified* for
every Git version; test on the minimum supported Git, §7).

## 4. Attributing changes in a shared checkout

The raw diff between two checkpoints contains everything that changed in the checkout during
the turn: this session's edits, other sessions' edits, the user's edits, build outputs that are
not ignored. To keep the per-turn diff honest:

- `files` = changed paths that this session's tool calls touched (edit/write/apply_patch paths
  from the turn's tool results) **plus** paths changed while one of this session's shell jobs
  ran, marked "via shell".
- `foreign` = the rest, counted; the diff view shows them collapsed under "also changed in the
  checkout during this turn (not by this session): N files".
- If `HEAD` moved during the turn (the user committed, pulled, switched branch), the diff is
  computed against a re-based baseline: `git diff <prev-tree-on-new-HEAD>` is not reliable, so
  the event records `head_moved: true` and the view says "HEAD moved during this turn
  (`a1b2c3d` → `e4f5a6b`); diff includes those changes". Do not try to subtract them (t3code
  excludes pulled files; not adopted in v1, §8 Q2).

## 5. Retention

- Refs live until: the session is deleted/purged (delete its `refs/nexus/checkpoints/<id>/`
  namespace), or the project has more than `[worktrees] checkpoint_keep` sessions with
  checkpoints (default 50; oldest-activity sessions pruned first), or a session has more than
  500 checkpoints (oldest pruned, keeping `-base`).
- Pruning only deletes refs; objects are reclaimed by the user's normal `git gc`. Nexus never
  runs `gc`.
- `refs/nexus/*` are not pushed by default (`git push` pushes branches/tags only) and not
  fetched by others. Documented, because `git push --mirror` would push them.

## 6. Restore (worktree sessions only)

For sessions whose root is a Nexus worktree, "Undo this turn" creates a revert on the session
branch: `git revert --no-edit <turn commits, newest first>` in the worktree, with trailers
`Nexus-Revert-Of-Turn: <seq>`. Refused if the worktree has uncommitted changes or a later turn
touched the same files (offer "revert this and later turns"). Emits `worktree.committed
{by: "harness", revert_of: seq}`. Main-checkout and external sessions get **no** restore
button; the diff view says "restore is available only for sessions in their own worktree".

## 7. Failure modes and tests

| Failure | Behavior |
| --- | --- |
| Not a Git project | `checkpoints` off, no events (one `checkpoint.skipped (not_git)` per session, not per turn) |
| Repo has no commits (unborn HEAD) | `commit-tree` without `-p`; diff vs empty tree |
| Index locked by the user | not affected (temp index); if `write-tree` fails, `checkpoint.skipped (git_error)` |
| Huge untracked dirs not ignored | size guard → `too_large`, shown once per session with the top 5 dirs by count |
| Submodules | recorded as gitlinks (the submodule commit), contents not snapshotted; stated in docs |

Tests: `tests/test_worktree_checkpoints.py` against real temp repos:
- HEAD, the real index, branches, stash and working tree are byte-identical before and after
  capture (compare `git rev-parse HEAD`, `sha256(.git/index)`, `git stash list`, file hashes);
- concurrent capture by two sessions;
- attribution of own vs foreign changes; shell-job changes marked;
- unborn HEAD; size guard; pruning; refs removed on session purge;
- revert for worktree sessions, and refusal paths;
- minimum Git version check (`git --version`, recommend ≥ 2.35 for `worktree` and temp index
  behavior; *not verified*).

## 8. Open questions

1. Default `checkpoints = true` for main-checkout sessions (recommended) or opt-in? Cost: one
   `git add -A` against a temp index per changing turn; slow on very large repos. Mitigation:
   auto-disable with a notice when one capture takes > 5 s.
2. Subtract changes pulled in by `HEAD` moves (t3code) — later.
3. Store checkpoints outside the user's repo (opencode keeps a separate gitdir under its data
   dir) to avoid any ref in the user's repository? Recommended: keep refs in the repo (cheap,
   shares objects); revisit if users object.

## 9. Docs

`docs/sessions.md` (checkpoints), `docs/events-and-view.md`, `docs/config.md`,
`docs/security.md` (refs in the user's repo; `--mirror` caveat), `docs/decisions.md` (hidden
refs; no restore in a shared checkout).
