# 06. Merge queue, sync and landing

Phases: **F6** (local queue, sync, base-moved notices, `merger` agent) and **F7** (PR mode).
Depends on F3.

## 1. The model, and how t3code does it

The user's model: three sessions work in three worktrees. They land **one by one**. When the
first lands, the second must bring those changes in (sync) before it can land, so the second
session's agent **knows there are incoming changes** and can adapt.

t3code (read at `96bae54`, not run) does **not** land locally:

- Work leaves a worktree through `commit | push | create_pr | commit_push | commit_push_pr`
  (`packages/contracts/src/git.ts:18-24`, `apps/server/src/git/GitManager.ts`), with
  model-written commit messages and PR text. Merge happens on the forge (`PullRequestService.ts`:
  merge, auto-merge, update-branch).
- No local ordering of parallel worktrees; ordering is the forge's merge queue or PR stacks.
- The only "base moved" signal: the `watch_pull_request` agent tool, which polls every
  2 minutes and wakes the agent when its PR starts to conflict (`docs/user/source-control.md`).
- `git merge` appears only as `--ff-only` to refresh a checked-out branch
  (`apps/server/src/vcs/GitVcsDriverCore.ts:3974`).

Nexus differs: a **local merge queue** is the default (`land = "local"`, D11), so a single
developer with no forge gets ordering; **base-moved notices are pushed** by the daemon the
moment a sibling lands, with the incoming files and the overlap (D13); sync is a **merge of the
target into the session branch** (D12); the target gets a **squash** commit by default (D14).
PR mode (§8) reuses the queue for ordering and notices and lets the forge merge.

## 2. Contract

- A **queue** exists per `(project, target branch)`. At most **one land** runs at a time per
  queue (a lock in the service root, so it holds across daemons of different installs too).
- A session can land only if: its worktree is clean (after a harness commit), no merge/rebase is
  in progress, it is **up to date** (the target tip is an ancestor of the session branch), and
  the user confirmed an exact review with a single-use token.
- Landing changes the user's target branch with a compare-and-swap ref update. If the target
  branch is checked out in the main checkout, only a fast-forward of a **clean** main checkout
  is allowed; otherwise Nexus refuses and says why. Nexus never resets, stashes or checks out in
  the main checkout.
- When a land succeeds, every other session targeting that branch gets a `worktree.base_moved`
  event at once and a context note at its next turn.
- The agent never lands. Landing is a user action (`Land…`). The agent can sync.

## 3. Data model

### 3.1 Queue state

Daemon state per `ProjectHost`, reconstructed on load from session logs plus service-root
records (no UI-only state, AGENTS.md rule 5):

```
LandQueue{project, target}:
  entries: list[QueueEntry]            # ordered by land_requested ts; ≤ 64
  holder: str | None                   # session id holding the land lock
QueueEntry{session, state, requested_at, behind: int, overlap: int,
           checks: "none"|"running"|"passed"|"failed", reason: str}
state ∈ "idle" | "behind" | "syncing" | "conflicts" | "checks" | "ready"
        | "awaiting_confirm" | "landing" | "landed" | "refused"
```

Lock file: `<service root>/locks/land-<sha8(target)>.lock` (flock, non-blocking acquire with a
bounded wait of 30 s, then the entry stays `ready` and retries when the holder releases).

### 3.2 Events (session log; bounded)

| Event | Payload |
| --- | --- |
| `worktree.base_moved` | `target`, `old_tip`, `new_tip`, `landed_by` (session name, or `"outside nexus"`), `subject`, `files` (≤ 200, clipped count), `overlap` (≤ 200: files both changed), `behind` (commit count) |
| `worktree.sync_started` / `worktree.synced` | `target_tip`, `merge_sha`, `conflicts_resolved` (count), `files` |
| `worktree.sync_conflict` | `target_tip`, `files` (≤ 200) |
| `worktree.land_requested` | `target`, `mode` (`squash`/`merge`/`ff`), `land` (`local`/`pr`) |
| `worktree.land_checks` | `command` (argv display), `exit`, `duration_ms`, `output_tail` (≤ 8 KiB) per check |
| `worktree.land_review` | `review_id`, `digest`, `files`, `+/-`, `commits` (≤ 200 subjects) |
| `worktree.land_refused` | `reason` (enumerated, §6), `detail` |
| `worktree.landed` | `target`, `old`, `new`, `mode`, `commit_count`, `files`, `+/-`, `landed_ref` |
| `worktree.pr_opened` | `remote`, `branch`, `url`, `number` (F7) |

View: `ConversationView.worktree.{behind, overlap, queue_state, last_land}`; the timeline shows
each event as a labelled row (file 08).

## 4. Detecting that the base moved

Sources, all funnelled through one `TargetWatcher` per `(project, target)`:

1. **A Nexus land** (§5 step 9): immediate.
2. **The user moved the branch outside Nexus** (committed in the main checkout, pulled, merged by
   hand): detected by reading `refs/heads/<target>` (a file read or `git rev-parse`, cheap)
   at: turn start of any session on that target, `SessionWorktreeStatus` calls, and every
   `[worktrees] watch_interval` (default 30 s) while the project is loaded and has a session on
   that target. `landed_by = "outside nexus"`.
3. **PR mode**: after `git fetch <remote> <target>` (on demand, or `fetch_interval`, default
   off); compares `refs/remotes/<remote>/<target>`.

For each session on that target whose branch does not contain the new tip:

- compute `behind = git rev-list --count <session>..<target>` and the incoming file list
  `git diff --name-only <merge-base> <target>` (bounded), and `overlap` = incoming ∩ files the
  session branch changed since its merge base;
- append `worktree.base_moved` to that session's log (idle or running; it is just an event);
- queue state → `behind`.

**Context note** (D13): at the session's next turn start, if there are unacknowledged
`base_moved` events, the context gets one harness note (bounded 2 KiB, fenced like other
harness notes, labelled in the context view):

```
The target branch `main` moved while you worked (3 commits; landed by session "brisk-teal-otter":
"Add retry to fetch client"). Changed files (12): src/net/fetch.py, …
Files you also changed (2): src/net/fetch.py, tests/test_fetch.py
You must sync before this session can land (`worktree_sync`). Sync now if you are about to
touch these files.
```

A note is emitted once per base movement; later movements are merged into one note.

## 5. Land algorithm (local mode)

Triggered by the user (`SessionLandPreview`, then `SessionLandConfirm`). Steps 1–6 are the
preview; 7–10 the confirm.

1. **Preconditions** (refuse with the reason if any fails): project is Git; session root is a
   Nexus worktree; session idle (no running or queued turn — offer "land after the queue
   drains"); worktree lifecycle `active`.
2. **Commit leftovers** (file 03 §4.1). Verify the branch is `nexus/session/<slug>` and not
   mid-merge. If the session branch no longer contains its recorded `base_sha` (history
   rewritten by the agent), continue but flag it in the review.
3. **Up to date?** `git merge-base --is-ancestor <target tip> <session tip>`. If not: state
   `behind`; the preview returns `needs_sync: true` with the `base_moved` summary and offers
   **Sync** (§7). Landing a behind session is not possible (D13).
4. **Checks** (if `land_checks` is set): run each argv in the session worktree with
   `land_checks_timeout` (default 600 s, max 3600), process slots, output streamed as labelled
   rows, recorded as `worktree.land_checks`. Project Git hooks (`pre-commit`, `pre-push`) do
   **not** run here (*decision*, §10 Q3). Failed checks → state `checks`, preview shows them;
   the user may confirm anyway (the confirmation records `checks_overridden: true`).
5. **Review**: build a frozen review of `<target tip>..<session tip>` by reusing
   `nexus/agents/worktree_review.py` (limits ≤ 500 files, 16 MiB/file, 128 MiB; `review_id`,
   `digest`), plus the commit list (subjects, trailers, by-agent) and the session's turn index.
   Over the limits: review shows summary only, and landing requires typing the session name in
   the confirmation (no silent acceptance).
6. **Message**: for `squash`, the `merger` agent (§9) writes the subject and body from the
   commit list and turn prompts; deterministic fallback when no model is available:
   subject = session title, body = commit subjects. Editable in the TUI (≤ 8 000 chars).
   Trailers added by Nexus: `Nexus-Session: <name> (<id>)`, plus `Co-authored-by:` lines are
   **not** added (*decision*, §10 Q4).
   Preview returns: review id, digest, message, checks, `target_checked_out: bool`,
   `main_clean: bool`, and a confirmation token (HMAC, 120 s, single use, bound to session,
   review id, digest, target tip and session tip — the existing pattern in
   `nexus/host/facade.py:418-650`).
7. **Confirm**: verify token; re-read target tip and session tip; if either changed since the
   preview, refuse (`stale_preview`) and return to step 3.
8. **Acquire the queue lock**; state `landing`; append `worktree.land_requested`.
9. **Apply** (compute the new target commit, then move the ref):
   - `squash`: `new = commit-tree <session tree> -p <target tip> -F <message>` with the user's
     identity as author and committer.
   - `merge`: `new = commit-tree <session tree> -p <target tip> -p <session tip> -F <message>`
     (valid because the session already contains the target, so its tree is the merge result).
   - `ff`: `new = <session tip>`.
   - Journal `{target, old: target tip, new, session}` to
     `<service root>/transactions/land-<id>.json` (fsync) **before** moving anything.
   - If `<target>` is checked out in the **main checkout**: require it clean (porcelain v2,
     untracked included, bounded) and not mid-operation; then
     `git -C <main> merge --ff-only <new>` (updates ref, index and files together). If not
     clean: refuse (`main_dirty`, with counts and the first 20 paths): "commit or stash your
     changes in the main checkout, then land again; or land to a branch that is not checked
     out".
   - If `<target>` is checked out in **another** worktree (user-made): refuse (`checked_out_elsewhere`,
     path shown).
   - Else: `git update-ref refs/heads/<target> <new> <old>` (compare-and-swap).
   - `git update-ref refs/nexus/landed/<session-id>/<n> <session tip>` (audit).
   - Delete the journal. Append `worktree.landed`. Release the lock.
10. **Fan out** §4 to the other sessions on `<target>`, then cleanup (file 07 §2: remove the
    worktree if `remove_on_land`; keep the branch and the landed ref).

If the session keeps working after landing (a new turn), it continues on the same branch, now
up to date with the target; its next land starts from the new target tip.

## 6. Refusal reasons (enumerated, shown verbatim in the TUI)

`not_git`, `no_worktree`, `session_busy`, `behind_target`, `sync_conflicts`, `merge_in_progress`,
`wrong_branch`, `stale_preview`, `token_invalid`, `main_dirty`, `main_mid_operation`,
`checked_out_elsewhere`, `target_moved` (CAS failed), `queue_busy` (lock wait timed out; stays
queued), `review_too_large_unconfirmed`, `pr_push_failed` (F7). Each has a one-line explanation
and a suggested action in `ui_support/worktree_text.py` (new).

## 7. Sync

Triggered by the agent (`worktree_sync()` tool, file 03 §4.4) or the user (`Sync` action, which
enqueues a turn in that session with `origin = "sync"` running the `merger` instructions).

1. Session idle (user action) or within a turn (tool). Commit leftovers.
2. `git merge --no-ff --no-edit -m "sync: <target>@<short tip>" <target tip>` in the session
   worktree, with trailers `Nexus-Session`, `Nexus-Turn-Seq`, `Nexus-Agent`.
3. Clean: append `worktree.synced`; mark all earlier `base_moved` acknowledged; state `idle`.
4. Conflicts: **leave them in the worktree**; append `worktree.sync_conflict {files}`; the tool
   result lists the conflicted files with hunks (bounded, via the existing diff projection) and
   says "resolve, run the project's checks, then the next commit completes the sync". The
   harness turn-end commit completes it (`git commit --no-edit` when `MERGE_HEAD` exists and no
   conflict markers remain in the conflicted files; else `commit_skipped
   (conflict_in_progress)` and state `conflicts`).
5. The user can abort a sync from the session actions (`git merge --abort`, confirmed).

Why merge, not rebase (D12): per-turn commits keep their SHAs, so `worktree.committed` events
remain valid, and a conflict is resolved once rather than once per commit. The target still
gets linear history because land squashes by default.

## 8. PR mode (F7)

`[worktrees] land = "pr"`:

- Steps 1–6 as local (checks and review still run locally; the merger writes **title and body**).
- Confirm → `git push <remote> nexus/session/<slug>:<pr_branch>` where `pr_branch` defaults to
  the session branch name (`[worktrees] pr_branch_prefix` to change it); then
  `gh pr create --base <target> --head <pr_branch> --title … --body-file -` (argv, no shell).
  Credentials stay with `git`/`gh`; Nexus never reads them. `gh` missing → refuse with the
  install hint; push needs the user's confirmation every time (no "always allow").
- Append `worktree.pr_opened {url, number}`. Queue state `landed` means "PR open"; the forge
  merges.
- Base movement comes from fetches (§4 source 3). When the target advances on the remote, the
  same `base_moved` notices go out; sync merges `<remote>/<target>`; a re-push updates the PR
  (confirmed).
- Squash vs merge is the forge's choice; `land_history` is ignored in PR mode (stated).
- Non-GitHub forges: out of scope for v1 (*decision*, §10 Q5).

## 9. The `merger` agent

Built-in agent definition `nexus/agents/data/merger.md` (`contexts: [root, subagent]`,
`isolation: none`, tiers `[medium, high]`, tools: read, grep, glob, edit, write, apply_patch,
shell, `worktree_status`, `worktree_sync`, `worktree_commit`; no `subagent`, no web). Used:

- as the role of a **Sync** turn in the session (§7), with instructions: understand the
  incoming changes (the base-moved note), merge, resolve conflicts preserving both intents, run
  `land_checks` (or the project's test command it can infer, reported as such), commit;
- as a **message writer** at land preview (§5 step 6), as a short subagent run with no tools
  except read-only Git views, given the commit list, turn prompts and diff stat.

It never lands, never pushes, never touches the main checkout (its `PathGuard` is the session
root).

## 10. Open questions

1. **Land while other sessions are mid-turn on the same target**: allowed (recommended; they get
   `base_moved` immediately and adapt at their next turn) vs block.
2. **Auto-sync**: never automatic (recommended, D13) vs sync idle behind sessions automatically
   when there is no overlap (could be a later setting `auto_sync = "no_overlap"`).
3. **Project Git hooks at land**: run `pre-commit` on the squash? Recommended no for v1; teams
   put what matters in `land_checks`. PR mode leaves it to CI.
4. **Attribution**: squash authored by the user with only `Nexus-Session` trailer (recommended)
   vs `Co-authored-by: Nexus`.
5. **Forges**: GitHub via `gh` only in v1 (recommended); GitLab (`glab`) later.
6. **Stacked sessions** (session B based on session A's branch): out of scope; B's base must be
   a user branch in v1.
7. **Target checked out in the main checkout and dirty**: refuse (recommended) vs land to the ref
   and leave the main checkout behind (would make `git status` show the user's checkout as
   behind/diverged; confusing).

## 11. Failure modes

| Failure | Behavior |
| --- | --- |
| Crash after journal, before ref move | recovery: target still at `old` → discard journal, entry back to `ready` |
| Crash after ref move, before `worktree.landed` | recovery: target at `new` → append `worktree.landed (recovered: true)`, fan out, delete journal |
| Target ref at neither `old` nor `new` | append `worktree.land_refused (target_moved)`, keep the journal for Doctor, entry `behind` |
| `merge --ff-only` in the main checkout fails midway | Git leaves the checkout unchanged on failure (ff is atomic for the ref; files may be partially updated on I/O error — *not verified*); report Git's message and point to `git status` |
| Two daemons (two installs) land to the same target | the flock serializes; CAS catches anything else |
| Checks hang | timeout kills the process group; recorded as failed |
| The merger edits files outside the merge | it is a normal turn in the session; its changes are in the next commit and in the review |

## 12. Tests

- `tests/test_worktree_sessions_land.py` (real temp repos, scripted provider):
  - A, B, C on `main`; A lands (squash): target has one new commit with trailers; B and C get
    `base_moved` with correct `files`/`overlap`; B's next turn context contains the note once;
  - B cannot land until synced; `worktree_sync` clean path; conflict path leaves markers,
    harness commit completes after resolution;
  - CAS: target moved between preview and confirm → `stale_preview`; between lock and update →
    `target_moved`;
  - target checked out in a clean main checkout → ff updates files; dirty → `main_dirty` with
    counts; checked out in a user worktree → `checked_out_elsewhere`;
  - `merge` and `ff` modes; landed refs; review too large requires typed confirmation;
  - checks fail → shown, override recorded;
  - crash injection at each journal point → recovery outcomes above;
  - outside-Nexus movement detected at turn start and by the watcher interval (fake clock).
- `tests/test_worktree_sessions_pr.py` (F7): fake `git` remote (bare repo in temp dir), fake
  `gh` on `PATH` recording argv; push confirmation required; fetch-based base moved.
- `tests/test_merger_agent.py`: definition loads, tool set, message fallback without a model.
- Native PTY journeys: Land with review and confirm; Sync from a `behind` badge.

## 13. Docs

`docs/decisions.md` (amend the F3 entry: user branches change only at a confirmed land via CAS
or ff of a clean checkout; squash default; local queue), `docs/agents.md` (`merger`),
`docs/host.md` (queue commands), `docs/sessions.md` (landing), `docs/config.md`,
`docs/security.md` (push and `gh`), `docs/ratatui-parity.md`, `docs/cli.md`
(`nexus land`, `nexus sync`, if added, file 08 §4).
