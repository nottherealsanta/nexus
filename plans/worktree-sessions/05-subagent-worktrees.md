# 05. Writer subagents in worktrees

Phase: **F5**. Depends on F3.

## 1. Contract

- Inside a session that runs in its own worktree, a **writer** subagent gets its own child
  worktree on `nexus/agent/<session-slug>/<child-id>`, branched from the **session branch tip**.
  Read-only subagents never get one (D3).
- The child has the shell, with cwd pinned to its worktree (D9).
- When the child finishes, its branch is **merged automatically into the session branch** (D8).
  On conflict, the merge is aborted and the parent agent gets a labelled result telling it to
  resolve.
- No user approval is needed for any of this: all branches involved are Nexus-owned. The user
  approves only when the session lands on their branch (file 06).
- `[worktrees] subagents = false` turns child worktrees off: writers work in the session's root,
  as today.

## 2. Today (verified)

- The `subagent` tool has a per-call `worktree: bool` (`nexus/tools/builtin/task.py:146`,
  validated `:318-322`). No config key and no frontmatter key decide it.
- Worktree children get `WORKTREE_CHILD_TOOLS` (`nexus/agents/runner.py:208`): no shell, no
  subagent. They cannot be resumed or continued (`runner.py:2369`, `:2532`).
- Their work returns via review → acknowledge → integrate (frozen bytes into a clean parent)
  (`nexus/agents/worktree_integrate.py`).
- Subagents are **blocking** (recent commit `17cd8f4`, `docs/agents.md:404-410`): parallel
  children come from several `subagent` calls in one tool batch (`concurrency="parallel"`).
- Shipped roles: `task`, `quick` (edit), `scout`, `advisor` (read; edit only when asked)
  (`nexus/agents/data/*.md`). Only the root spawns (`docs/decisions.md:80`).

## 3. Policy: who gets a worktree

New agent-definition frontmatter key `isolation: worktree | none | auto` (default `none`):

| Value | Meaning |
| --- | --- |
| `worktree` | always a child worktree when the parent session is in a worktree and `subagents = true` |
| `auto` | a child worktree only when **another writer** child of the same session is running at spawn time (parallel writers); a lone writer works in the session worktree directly |
| `none` | never |

Shipped defaults: `task: worktree`, `quick: auto`, `scout: none`, `advisor: none`. Validation at
load (`nexus/agents/model.py`, *not verified* exact file): a definition whose tool set has no
mutating tool cannot declare `worktree`/`auto` (error in Settings → Agents, definition still
loads with `none`).

Per-call flag: the `subagent` tool's `worktree` argument becomes `worktree: bool | None = None`
(None = follow the role). `true` on a role with `none` is refused with a reason ("scout is
read-only"); `false` on a `worktree` role is allowed (the parent may choose to share its tree).
Prompt guidance in `orchestrator.md`/`build.md`: "Parallel writers get their own worktrees and
are merged into your branch when they finish; you will be told about conflicts."

Sessions in the **main checkout** (or external roots): writer subagents do **not** get
worktrees by default (there is no Nexus-owned session branch to merge into). An explicit
`worktree: true` keeps today's legacy flow (review/acknowledge/integrate, §7). Recommended
(§9 Q1).

## 4. Child worktree lifecycle

### 4.1 Spawn

In `SubagentRunner` where `WorktreeService.create` is called today (`runner.py:2137-2172`):

1. Take the **session root write lock** (new, per session worktree; also taken by the parent's
   mutating tools and by harness commits, so the parent's tree is quiescent).
2. If the session worktree has uncommitted changes, harness-commit them first:
   subject `nexus: checkpoint before <child-id>`, `Nexus-Agent: root` (file 03 §4.1). The child
   must see what the parent sees.
3. `git worktree add -b nexus/agent/<session-slug>/<child-id> <service root>/worktrees/<slug>
   <session tip>`. No clean-parent requirement (the parent is the session branch, now committed).
4. Setup (file 07 §1) with `setup_for_subagents` (default `copy_only`: copy files, skip the
   setup command; dependencies like `node_modules` are shared by symlink if
   `[worktrees] share_dirs = ["node_modules"]` is set — *decision*, §9 Q2).
5. Release the lock. Record `kind = "subagent"`, `parent_record`, `lifecycle = active`. Emit
   `agent.spawned` with `worktree {branch, base_sha}` as today.

### 4.2 Tools

`WORKTREE_CHILD_TOOLS` gains `shell` (and `code`). The child's `PathGuard` keeps
`for_worktree` semantics (writes only in the child worktree; the session worktree and the main
checkout are read-only). The shell cwd starts at and is bounded by the child root. Jobs are
killed when the child finishes (as today for children). Process slots apply (file 01 §6).

### 4.3 Commits inside the child

Same as file 03 §4: the child may call `worktree_commit`; at the child's end the harness commits
leftovers with `Nexus-Agent: <child-id>`, `Nexus-Turn-Seq: <parent turn seq>`,
`Nexus-Session: <root session>`. Child commit events are recorded in the **child's** log
(`worktree.committed {by: "subagent"}`) and summarised in the parent's subagent result row.

### 4.4 Auto-merge on finish

When the child's run ends (success, failure or cancel, but not `escalate`-blocked; see below):

1. Take the session root write lock.
2. If the child made no commits: remove the child worktree and branch; result says
   "no changes".
3. Else, in the **session worktree**: harness-commit any parent leftovers, then
   `git merge --no-ff --no-edit -m "nexus: merge <child-id> (<role>)" <child branch>` with
   trailers `Nexus-Session`, `Nexus-Turn-Seq`, `Nexus-Agent: root`, `Nexus-Merged-Agent:
   <child-id>`.
4. Clean merge: emit `worktree.subagent_merged {child, merge_sha, commits, files, +/-}` in the
   parent log; remove the child worktree (keep the branch until the session lands, for audit;
   file 07 §2); the subagent tool result gets a labelled "Merged into your branch: N commits,
   M files" section.
5. Conflict: `git merge --abort`; emit `worktree.subagent_conflict {child, files (≤ 200)}`; keep
   the child worktree and branch; the result says: "Merge conflict in N files (list). Run
   `worktree_merge_child('<child-id>')` to merge and resolve the conflicts in your worktree."
6. Release the lock.

Failed or cancelled children: their partial work is kept on the child branch and **not**
merged; the result says "not merged (child failed); `worktree_merge_child` to merge anyway or
`worktree_discard_child` to drop".
Escalated (blocked) children: not merged; kept for continuation (§6).

Parallel children of one batch finish in any order; merges are serialized by the lock, so the
second child's merge sees the first child's changes. Conflicts between siblings surface as a
conflict on the later one.

### 4.5 Parent tools

| Tool | Does |
| --- | --- |
| `worktree_merge_child(child_id)` | Merges a kept child branch into the session worktree and **leaves conflicts in place** for the parent to resolve with its normal tools; the next harness commit completes the merge (`git commit --no-edit` when `MERGE_HEAD` exists and no conflict markers remain). Refused if the parent tree is dirty with unrelated changes (commit first). |
| `worktree_discard_child(child_id)` | Removes the child worktree and branch; records it. Mutating, approval applies. |

Both appear only in sessions with a worktree root.

## 5. Interaction with the parent's own edits

- While a blocking subagent batch runs, the parent's other calls in the same batch may include
  mutating tools. They take the session root write lock per call, so a child merge never runs
  while a parent edit is half-done; ordering between them is lock order. Documented, not
  prevented.
- `auto` isolation decides at spawn: if no other writer child is active, the writer shares the
  session worktree (no branch, no merge) and its edits are attributed by `Nexus-Agent` in the
  turn-end commit's body ("files by <child-id>: …") — *not verified* feasible without per-file
  ownership; fallback: the turn-end commit lists agents that wrote, without per-file split.

## 6. Continuation and resume

Today worktree children cannot be continued (`runner.py:2369`). With session-owned branches:

- A finished, merged child: continuation (`subagent_send`, `docs/decisions.md:1389` v1 rules)
  creates a **new** child worktree from the current session tip and the same role; the child's
  log continues. The old branch is not reused.
- An escalated (blocked) child keeps its worktree; continuation resumes in it.

## 7. Legacy flow

- Existing v2 records (`nexus/subagent/*`) and the review/acknowledge/integrate commands stay
  working until drained. `/worktrees` lists them as "legacy subagent worktrees".
- The per-call `worktree: true` in a main-checkout session keeps using the legacy flow (§3).
- Removal: one minor release after F6, with a `docs/decisions.md` entry, if no legacy records
  remain in use (Doctor reports counts).

## 8. Failure modes

| Failure | Behavior |
| --- | --- |
| Child worktree creation fails | the `subagent` call fails with the reason; nothing left behind; parent decides |
| Session root lock held too long (a stuck parent tool) | lock wait bounded at 120 s, then the merge is deferred: result says "merge pending; will retry at your next turn boundary" and the harness retries at turn end |
| Child switched branches or rewrote history | merge uses whatever `refs/heads/nexus/agent/...` points at; if the child's branch no longer descends from its base, the result notes "history rewritten" and merges anyway |
| Daemon crash mid-merge | recovery: if `MERGE_HEAD` exists in the session worktree and the log has no `subagent_merged` for it, `git merge --abort` and re-run §4.4 (file 07 §3) |
| `max_worktrees` reached | child shares the session worktree (`auto` behavior) and the result says so; `worktree` roles get a refusal instead |

## 9. Open questions

1. Writer children in main-checkout sessions: none by default + legacy on request (recommended)
   vs base them on a checkpoint commit and land through the legacy review.
2. Dependency dirs in child worktrees: copy-only setup + optional `share_dirs` symlinks
   (recommended) vs full setup per child (slow, correct).
3. Merge style into the session branch: `--no-ff` merge commits (recommended, keeps
   per-child grouping visible) vs rebase/cherry-pick.
4. Should a failed child's partial work be merged? Recommended no (kept, parent decides).

## 10. Tests

- `tests/test_worktree_sessions_subagents.py`: role policy (`worktree`/`auto`/`none`, per-call
  override and refusals, toggle off); spawn commits parent leftovers first; child gets shell
  pinned to its root; two parallel writers both merge, second sees first; conflict path keeps
  branch, `worktree_merge_child` leaves markers, next harness commit completes; failed child not
  merged; discard; crash mid-merge recovery.
- Peers: `tests/test_builtin_task.py` (argument shape), `tests/test_agents_manager.py`
  (frontmatter), `tests/test_subagent_continuation.py` (new child worktree on continue).

## 11. Docs

`docs/agents.md` (isolation, child shell, merge, legacy), `docs/tools.md` (new tools, child tool
set), `docs/security.md:108,135` (children now have a shell in worktrees), `docs/decisions.md`
(children merge into Nexus-owned branches without user review; supersedes the per-child
integrate for session worktrees).
