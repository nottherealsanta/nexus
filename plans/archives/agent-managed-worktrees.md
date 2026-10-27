# Plan: The agent manages its own worktrees

Status: implementation is present across all three phases; verification is incomplete
(2026-10-24). Runtime, tools, prompt, permission, storage, host and UI behavior described below
have landed, as have the corresponding documentation updates. The full suite is not green: its
first run had 39 failures; a later backend-targeted run had 6 failures (details in §6). Several
focused storage/UI runs passed, but that does not establish full-suite completion. Builds on the
implemented worktree-sessions ledger (`plans/worktree-sessions/`, 02, 03, 06, 08).

## 1. Intent

Today a new session in a Git workspace asks the user, before the first turn,
"Create a worktree, or work in the main checkout?". The answer depends on the
worktree mode (`none` / `ask` / `always`), and `ask` is the default. So the user
answers before they know what the work will turn into, and answers again for
every session.

This plan removes the modes and the question altogether. Every session starts in
the main checkout. From there the agent itself does four things, at any point,
including in the middle of a turn:

- **List** the project's worktrees.
- **Create** a worktree, and move into it.
- **Move** between the main checkout and any existing worktree. Several sessions
  can share one worktree.
- **Finish** a worktree: either merge it back into the base branch, or leave it
  as it is (the branch is kept, unmerged) and move on.

All of this happens in one session. When to use a worktree is guidance in the
agent's instructions. Nexus never forces the choice. When the agent decides to
merge, it merges; nobody approves it.

Every create, move and merge is a labelled timeline row with the agent's stated
reason.

A worktree belongs to the project, not to a session. A session is simply in one
checkout at a time, and any number of sessions can be in the same one, just as
several sessions already share the main checkout today.

```
main ──create("refactor auth")──▶ ⎇ brave-otter ──merge──▶ base ──move(main)──▶ main
                                                                                 │
          ⎇ calm-heron ◀──create("fix flaky test")───────────────────────────────┘
               │
               └── move(main), left unmerged: branch nexus/wt/calm-heron kept
```

### Non-goals

- Subagent worktrees. They stay disabled (`SUBAGENT_WORKTREES`, `docs/decisions.md:55`).
- A merge tool, merge queues or PR automation. Merging stays plain Git run by the
  agent: squash, then `git -C <main> merge --ff-only`, as today.
- A model-facing tool to delete a worktree. Only clean, merged checkouts are
  removed, automatically (3.5).
- Changing how worktree turn commits are made. This plan removes main-checkout
  checkpoints and adds hidden refs that keep turn commits alive after a squash
  (3.5.1).
- New web-app features (to be deprecated). It only drops its mode UI.

## 2. What already exists

Most of the mechanics are already there. This plan is mainly about policy,
instructions, a few tools, and UI.

- **Today a worktree is owned by one session.** It is an authenticated per-session
  record (`agents/session_worktrees.py`, v3 records, `docs/decisions.md:133`).
  A fork that inherits a worktree root gets its own new worktree
  (`worktree_sessions.py:344-350`). This plan replaces that 1:1 mapping (3.4).
- **The root can change mid-session.** `session.root_set` is replayed with the
  last event winning (`runtime.py:4577`, `view/reduce.py:2294+`).
  `set_session_root` (`runtime.py:4658`) refuses while a turn, queued input, job
  or shell is active.
- **Promote (main → new worktree)** is `SessionWorktrees._promote`
  (`worktree_sessions.py:915-1011`). It carries the session-owned changed files
  over and commits them.
  - The model already has a `worktree_promote {reason, continuation?}` tool.
  - The move is deferred to the end of the turn (`request_promote`, :1021).
  - The tool always asks for confirmation (`runtime.py:5304-5305`).
- **Detach (worktree → main)** exists as `detach()` (:1082) and as the host
  command `SessionDetach`. Nothing in the model tools or the TUI calls it.
- **Merge detection.** `_landing` (:1035) emits `worktree.merged` when the
  session head is an ancestor of the target.
- **Instructions.** `<session_worktree>` (`context/parts.py:594-630`) is
  rendered only when the root is a worktree. In the main checkout the agent gets
  no worktree guidance at all.
- **Modes, to be removed:**
  - `worktree-mode.json` and `SessionWorktrees.mode()` / `choice()`
    (`worktree_sessions.py:50-97`), and the `locked()` rule (:115).
  - The host commands `SessionWorktreeChoice` / `SessionWorktreeSet`
    (`host/protocol.py:430-441`).
  - The ask modal (`ui/ratatui/actions.py:571-601`, `worktree_pages.py`).
  - The `/worktree` modal and the settings row (`settings_pages/agents.py`).
  - The `[worktrees].sessions` key (`config/schema.py:1071`).
- **Gap.** The TUI top bar does not react to `session.root_set`
  (`worktree_bar.py`, `prototype.py:65-97`). It stays stale until the next tab
  switch.

## 3. Design

### 3.1 No modes

- **Removed.** The modes, the ask modal, the `/worktree` command and the settings
  row go, along with the draft "worktree selected" state. There is nothing for the
  user to choose up front.
- **Start.** Every session starts in the main checkout.
- **No locking.** The rule that locks the choice after the first turn goes away.
- **Breaking change, no backwards compatibility.** Nothing old is kept, read,
  converted or specially rendered:
  - `worktree-mode.json`, `[worktrees].sessions` and `[worktrees].checkpoints`
    are deleted outright.
  - The per-session v3 worktree records are replaced by project records (3.4).
  - The old event types (`checkpoint.*`, `session.worktree_set`) and their
    reducer and replay code are deleted. The `worktree_promote` tool is deleted
    with no alias.
  - Sessions and worktrees created before the change are not supported. They
    may fail to open or replay. Start new sessions.
  - Rule 5 (replay reproduces live state) applies to records written after the
    change.
- **Branches.** New worktree branches are `nexus/wt/<name>`.
- **Non-Git workspace.** The worktree tools are not offered and no
  `<session_worktree>` block is rendered.
- **Protocol.** Removing `SessionWorktreeChoice` and `SessionWorktreeSet` bumps
  the host protocol version.

### 3.2 Agent guidance

A `<session_worktree>` block is rendered in every Git session, in the main
checkout too. It is guidance only. Nexus enforces none of it: there is no forced
worktree, and no write in the main checkout is refused.

**Guidance (prompt text, kept short):**

- **Create a worktree** when you are about to change code across files, do a
  refactor or a dependency/build change, run anything experimental, or the change
  will take several turns. Prefer one when another active session is editing the
  main checkout.
- **Stay in the main checkout** for questions, reading, review, planning, running
  tests or commands, and small edits the user asked to make directly. Nothing
  is saved per turn there, so if you might want to review or undo your changes
  turn by turn, create a worktree.
- **Decide before your first edit.** You can create or move at any point, even
  mid-turn, and your changed files are carried over, but early is cleaner.
- **Check what exists first.** Call `worktree_list` before creating. Continue an
  existing worktree when the work belongs there.
- **Merge when done.** When the piece of work is complete (the request is met and
  the relevant tests pass), commit, squash and merge it into the base branch right
  away, using the steps in this block. Don't wait to be asked.
  - If the merge fails (the base has diverged, or there are conflicts), rebase in
    the worktree and retry.
  - If it still fails, stop and tell the user.
- **Or leave it as it is.** If the user wants to hold the work, or it is not
  ready, don't merge. Move back to the main checkout and say that the branch is
  kept unmerged.
- **One worktree per piece of work.** After a worktree is finished, move to the
  main checkout, or create a new worktree for the next, unrelated task.
- **The user wins.** If the user says "work here", "use a worktree" or "don't
  merge", do that.

**Facts (rendered every turn):**

- **Current root:** main checkout, or worktree name, branch and base.
- **Main checkout:** whether it is dirty.
- **Other sessions:** the names of other active sessions in this project whose
  root is the main checkout. This needs a host-side fact; reuse the source behind
  the TUI's `ownership.py`.
- **Current worktree:** whether it is merged or unmerged, and whether it is
  dirty.
- **Steps:** the existing merge steps (commit, squash, `--ff-only`, the
  diverged-base case).

### 3.3 Model tools

These replace `worktree_promote` and `worktree_status`. `worktree_commit` is
unchanged. All of them return labelled fields, not raw JSON, and their errors
tell the agent what to do next (opencode does the same).

**`worktree_list {}`** (read-only)
- **Lists:** every Nexus session worktree of this project, plus the main
  checkout.
- **Fields per row:**
  - name and branch;
  - base branch and base commit;
  - checkout present or removed;
  - dirty (yes/no);
  - merged, or unmerged with the count of unmerged commits;
  - the sessions in it now, each with its title and whether it is active;
  - the last activity time;
  - "← you are here" on the current row.
- **Bounded:** at most `worktrees.max_worktrees` (16), with the clipping
  announced.
- **Extra Git worktrees** (ones Nexus did not create) are listed separately, as
  "external, read-only info". The agent cannot move into them.

**`worktree_create {reason, name?, base?}`**
- **What it does:** creates a new worktree and moves the session into it. With
  no `name`, it gets a random word pair. `base` defaults to the current base
  branch.
- **From the main checkout:** this is today's promote. The session-owned changed
  files are carried over.
- **From a worktree:** the new worktree branches from the updated base. The
  worktree being left is handled as in `worktree_move`.
- **Limits:** refused at `max_worktrees`, with the error naming the merged
  worktrees whose checkouts could be cleaned up.

**`worktree_move {to, reason}`**
- **`to`:** `"main"` or a worktree name from `worktree_list`.
- **Moving into:** any Nexus worktree of the project, whoever created it and
  whoever else is in it. Nothing is transferred, and no other session is moved.
- **A removed checkout** is recreated from its branch, as `prepare` does today
  (`worktree_sessions.py:351-357`).
- **Leaving a worktree:**
  - Refused while it has uncommitted changes. The agent commits first, or
    `worktree_commit`s.
  - With unmerged commits it is allowed. The result and the timeline row say
    plainly that `nexus/wt/<name>` is kept unmerged.
  - The branch is never deleted.

**Merging** has no tool. The agent runs Git (3.4).

**Permissions**
- **The worktree tools never ask.** `worktree_list`, `worktree_create` and
  `worktree_move` only create or switch checkouts in Nexus-owned space, and both
  can be undone. The forced ask on today's promote is dropped.
- **Path permissions follow the root**, as today (`PathGuard.for_worktree`).
- **The merge never asks.** Today any shell Git write that uses `-C`, `--git-dir`
  or `--work-tree`, or follows a `cd`, is forced to ask (`permissions.py:1217-1250`).
  That rule gets one narrow exemption: Nexus recognises the merge the agent was
  told to run, and allows it, in every permission mode.
  - **What the exemption allows:**
    - `git -C <main> merge --ff-only <branch>`, where `<main>` is this project's
      main checkout path and `<branch>` is a Nexus worktree branch of this project;
    - optionally, `git -C <main> checkout <base>` / `switch <base>` first.
  - **Checking.** The command is parsed, not pattern-matched on text. Any other
    flag, extra command, redirect or pipe makes it fall back to the forced ask.
  - **Why this is safe.** `--ff-only` can only move the base branch forward to a
    commit that already contains it, so nothing on the base is lost.
  - **Squash.** The squash runs inside the worktree, which the worktree path
    guard already allows.
  - **Where it is recorded.** `docs/security.md`, as a deliberate change.
  - **Everything else** in the main checkout keeps the forced ask.

### 3.3.1 Moving in the middle of a turn

This follows opencode's `session_move` (3.9).

**Request, then apply at the next step boundary**
- `worktree_create` / `worktree_move` validate and return at once. They check
  that the target can be created or recovered, and that no background shell or
  job of this session is alive.
- The move is applied at the next step boundary: after every tool call of the
  current model step has finished, and before the next model request. The turn
  keeps going: there is no new turn, and the step count carries on.
- **Tool description warning.** "The move happens after this step. Do not run
  tools that depend on the new directory in the same step." Any other tool calls
  in the same step still run against the old root.

**Order of work at the step boundary**
1. Close the old root's part of the turn: a `worktree.committed` if the session
   is leaving a worktree. Nothing is saved for the main checkout.
2. Create or recover the target, and carry over the owned changes (create from
   the main checkout only).
3. Emit `session.root_set` plus the `worktree.*` events.
4. Rebuild the per-root state before the next model request: the path guard,
   the worktree tool set, the shell cwd and the cached root.
5. Open the new root's part of the turn: `worktree.turn_started` if the target
   is a worktree.

**Barrier.** Compaction and queued user steers never run between the request and
the move (as in opencode `inbox.ts`).

**Runtime change.** `set_session_root` (`runtime.py:4658`) gets an in-turn path
that only the owning turn can use. The user-facing host commands keep their
idle-only rule.

**The model sees the move.** A durable in-band notice is added to the
conversation at the boundary, built from `session.root_set` and kept through
compaction. For example: "Working directory is now ⎇ brave-otter (`<path>`,
branch `nexus/wt/brave-otter`, base `main @ 1a2b3c`)". `<session_worktree>`
is also re-rendered. The tool list can change between steps. Check that each
provider tolerates this, and that prompt caching only loses the tail
(`docs/context.md`).

**Turn diff and revert.** These cover only the worktree part of a turn: from
step 5 to the end of the turn, or from the start of the turn to step 1. A part
spent in the main checkout has nothing saved (3.5.1). A range that spans two
worktrees is refused: "This turn moved between checkouts; view each part
separately."

**Recovery.** If the current root vanished and the session is idle, a requested
move applies immediately (opencode `move.ts`).

**Failure**
- If any step before 3 fails, the root is unchanged, and the failure is added as
  a notice the model sees.
- After step 3 the move stands. A failure in step 4 or 5 is reported, and the
  next tool call re-runs `validate_session_root`, which fails closed.

### 3.4 Sharing a worktree between sessions

A worktree is a project-level thing. Sessions point at it through their
`session.root_set`, many to one. There is no owner, no transfer and no lock.

**Records**
- The authenticated worktree record (v3) drops its owning `session_id`. It
  becomes a project worktree record: name, branch, base, path and created-by
  (the creator is kept for display only).
- `validate_session_root` checks that the record belongs to this project and is
  active, not that the session owns it.
- Who is in a worktree is derived from the latest `session.root_set` of each
  session. It is a host-side index that is rebuilt from the records, so it
  survives restarts (rule 5).

**Forks** stay in the parent's root. The rule that "a fork gets its own new
worktree" goes away.

**Turn commits in a shared checkout**
- **What a commit holds.** The harness commit at the end of a turn (and at a move,
  3.3.1 step 1) commits only the paths this session changed in that turn. The
  tracking already exists: promote already copies the session-owned changed
  files. Another session's half-finished edits are never swept into it.
- **Commits from two sessions at once.** `update-ref` is already
  compare-and-swap (`docs/decisions.md:133`). On a lost race, the commit is
  rebuilt on the new head and retried, a bounded number of times.
- **Revert** of a turn is refused if a later commit from another session touches
  the same paths. The refusal names that session.

**The agent sees who else is there.** The `<session_worktree>` facts and
`worktree_list` name the other sessions in the current worktree and whether they
are active. The guidance says:
- If another active session is in this worktree, coordinate through the files
  and commits; do not revert or rewrite its work.
- If another active session is in this worktree, merge without squashing
  (`--ff-only` of the branch as it is). Squashing rewrites the branch under the
  other session.

**Merging a shared worktree.** Whoever merges, merges the branch as it is at
that moment. Every session in that worktree sees it as merged, through
`worktree.merged` and the facts. Later commits make it unmerged again.

**Not changed:** several sessions can already work in the main checkout at the
same time.

### 3.5 Merging

- **The agent merges with Git.** It commits, squashes, then runs
  `git -C <main> merge --ff-only`, as the block already describes. The guidance
  says to do it as soon as the work is done (3.2).
- **No Nexus command is needed.** As commit 11a3b67 states, merging requires no
  Nexus merge command.
- **Detection runs at every step boundary.** `_landing` now runs at each step
  boundary, not only at turn end. It emits `worktree.merged` once per merged
  head, so a `worktree_move` later in the same turn already knows the work is
  merged.
- **Leaving work unmerged is a valid finish.** `worktree_list` shows such
  worktrees as unmerged, so the same session or a later one can pick them up.
- **No approval.** The merge never asks (3.3, Permissions).
- **Manual fallbacks.** The `/merge` nudge and the user's "Integrate" action remain
  for when the agent stopped short.

#### 3.5.1 Per-turn history: worktrees only

**Main checkout: no checkpoints.** The main-checkout checkpoints are removed:
- the `checkpoint.started/captured/skipped` events are no longer emitted
  (`worktree_sessions.py:487-600`);
- the hidden checkpoint refs are no longer written;
- the `[worktrees].checkpoints` config key is removed (`config/schema.py:1076`).

**Why.** Any work worth undoing turn by turn belongs in a worktree. The guidance
says so: "If you might want to review or undo your changes turn by turn, create
a worktree."

**What the user gets in the main checkout:**
- Turn diff and turn revert are not offered there.
- The TUI and the host say why: "No saved turns in the main checkout; work in a
  worktree to get them."

| Checkout | What each turn saves |
| --- | --- |
| Main checkout | nothing |
| Worktree | a turn commit (`worktree.committed`) on the worktree branch, plus a hidden ref (below) |
| A turn that moves | the worktree part only (3.3.1 steps 1 and 5) |

**The gap: squashing drops turn commits.** "Merge when done" squashes the
worktree branch. After that, the per-turn commits are no longer reachable from
any branch. Turn diff and revert for those turns then depend on unreachable
objects, which `git gc` eventually prunes.

**Fix.** Each turn commit is also pinned under a hidden ref,
`refs/nexus/turns/<session>/<turn>`.
- **What it keeps:** a session's turn diffs keep working after a squash, after
  the merge, and after the checkout is removed (3.6).
- **What revert does after a squash:** revert of a squashed turn is refused,
  with the reason "already squashed and merged". Its diff can still be viewed.
- **Bounds:** the refs are capped per session, and are removed when the session
  is deleted (`docs/sessions.md`).

### 3.6 Cleanup of merged checkouts

The current decision is "no automatic cleanup". This plan narrows it:

- **When:** a worktree is merged and clean, and the last session in it moves
  away.
- **What is removed:** only the checkout, through the existing
  `worktree_cleanup` path.
- **What is kept:** the record and the branch, so the checkout can be recreated.
- **Never removed automatically:** unmerged or dirty checkouts.

Record the change in `docs/decisions.md`.

### 3.7 Several worktrees in one session's history

- **View.** `WorktreeView` becomes the *current* worktree plus a bounded list of
  past worktrees.
  - Each one has its name, branch, base, state (`merged` / `unmerged` /
    `removed`) and turn range.
  - It is keyed by `worktree_id`.
  - Replay must rebuild the same list (rule 5).
- **Timeline rows.** Each create, move and merge is one labelled
  row, built from durable events. For example: `⎇ Created worktree brave-otter
  · from main @ 1a2b3c · reason: "refactor auth across 6 files"`.

### 3.8 TUI

- **Removed.** The ask modal, `/worktree`, and the settings row.
- **Top bar.** It refreshes when the view reports a new `session.root_set`
  (compare `ConversationView.root`), not only on a tab switch or after a `!`
  command.
- **`/worktrees`.**
  - Shows the same list as `worktree_list`, from a host command shared with the
    tool.
  - Keeps "Integrate into base branch…" and "Remove checkout…".
  - Adds "Move this session here", which calls `SessionPromote` /
    `SessionDetach` (and a new "move to existing" command).
  - These let the user override the agent without typing a sentence. They are
    idle-only.
- **`/merge`** stays as a manual nudge, for when the agent stopped short.

### 3.9 Ideas from opencode (`references/opencode`)

opencode lets the model create, list and remove worktrees, and move its session
between directories. The tools are in `packages/core/src/tool/plugin/opencode.ts`;
moves are handled in `packages/core/src/session/move.ts` and `session/inbox.ts`.

| opencode | Taken here | Not taken |
| --- | --- | --- |
| `session_move`: queued, applied at a safe step boundary, step count kept (`runner/llm.ts:85-115`) | 3.3.1 | — |
| Tool description: "do not run destination-dependent tools in the same execute call" | Same warning on create/move | — |
| Durable `session.moved` + a `location-switched` message the model sees, kept through compaction | In-band notice from `session.root_set`; timeline row | — |
| Compaction and steers do not cross a pending move (`inbox.ts:493-550`) | 3.3.1 barrier | — |
| Turn diff refuses a range spanning a move (`session/diff.ts:117-140`) | 3.3.1 | — |
| An idle session whose directory vanished moves immediately (`move.ts:119-150`) | 3.3.1 recovery | — |
| `worktree_list` / `worktree_create`, with errors written for the model | `worktree_list` (with merge state and the sessions in each), `worktree_create` (creates *and* moves) | — |
| `worktree_remove {force}`, "only when the user has confirmed" | — | No model remove; merged clean checkouts are removed automatically (3.5) |
| A one-line "consider moving" nudge | — | Too thin; Nexus gives real guidance |
| Detached worktrees; no branches, no merge | — | Nexus keeps `nexus/wt/<name>` branches, turn commits and merge detection |

## 4. Phases

Implementation checklist (checked means implementation is present, not that every test passed):

- [x] Phase 1 — remove modes; agent create/list and in-turn move/history behavior.
- [x] Phase 2 — move/create between worktrees, timeline/UI and merged-cleanup behavior.
- [x] Phase 3 — project-shared worktrees, session index, scoped commits and cross-session moves.
- [x] Matching docs listed in §5 have landed in the worktree.
- [ ] Verification: full-suite and backend-targeted failures remain; see §6.

1. **Remove the modes; agent create and in-turn move.** (implementation present)
   - Remove the modes, the ask modal, `/worktree`, the settings row and
     `[worktrees].sessions` (3.1).
   - Render `<session_worktree>` in main roots with the guidance and facts.
   - Replace `worktree_promote` with `worktree_create`, with no alias.
   - Name new branches `nexus/wt/<name>`.
   - Add `worktree_list`.
   - Apply moves in-turn at the step boundary (3.3.1).
   - Run merge detection at step boundaries.
   - Drop the forced ask on create.
   - Remove the main-checkout checkpoints and `[worktrees].checkpoints`.
   - Pin worktree turn commits with hidden refs (3.5.1).
   - Add the merge exemption (3.3, Permissions).
   - Fix the top-bar refresh.
2. **Moving between worktrees in one session.** (implementation present)
   - Add `worktree_move` (to main, and to own worktrees), and `worktree_create`
     from a worktree.
   - Keep several worktrees in `WorktreeView`, and add the timeline rows.
   - Automatic cleanup of merged, clean checkouts.
   - Update `/worktrees`.
3. **Shared worktrees** (3.4; implementation present).
   - Make worktree records project-level.
   - Add the session-in-worktree index.
   - Commit only the session's own paths, retrying on a lost compare-and-swap.
   - Keep forks in the parent's root.
   - Allow `worktree_move` into any worktree.

   Until phase 3, `worktree_move` only enters the session's own worktrees.

## 5. Docs to update with the code

- [x] `docs/sessions.md` and `docs/tools.md`: tools and lifecycle; modes removed.
- [x] `docs/context.md`: `<session_worktree>` in main roots and in-band move notice.
- [x] `docs/config.md`: removed keys/mode file and `nexus/wt/` branch prefix.
- [x] `docs/host.md`: protocol and command changes.
- [x] `docs/security.md`: merge exemption from forced ask on Git writes.
- [x] `docs/agents.md`: forks stay in the parent's worktree.
- [x] `docs/cli.md`: `/worktree` removed; `/worktrees` updated.
- [x] `docs/ratatui-parity.md`: removed modal and top-bar root refresh.
- [x] `docs/decisions.md`:
  - "the agent manages its worktrees, with no modes" (supersedes the
    ask-by-default choice);
  - in-turn moves;
  - project-level worktrees shared by many sessions (supersedes the per-session
    record);
  - the merge needs no approval;
  - main-checkout checkpoints removed (supersedes `docs/decisions.md:162`);
  - the narrowed cleanup rule.
- [x] `plans/worktree-sessions/README.md`: points to this plan.

These entries are marked for documentation being present in this worktree, not as
independent verification of every documented behavior.

## 6. Verification

The repository artifacts record these aggregate runs and focused follow-ups:

- `artifacts/candid-tests.log`: full suite, 8,470 passed, 361 skipped, 39 failed,
  6 deselected. Failures include worktree/runtime/UI/storage cases; this is not a
  passing full-suite result. `test_host_daemon_projects` and
  `test_live_packaging` were confirmed pre-existing baseline failures.
- `artifacts/backend-targeted4.log`: backend-targeted worktree run, 120 passed,
  6 failed. Remaining failures: `test_phase3_exit.py`, all three
  `test_phase6_integration.py` worktree-child tests,
  `test_worktree_sessions_runtime.py::test_full_agent_flow_list_create_edit_merge_and_move_to_main`
  (expected `e2e.txt` absent), and
  `test_worktree_setup.py::test_setup_trust_rejects_a_stale_review_digest`
  (test expects an unredacted command, implementation returns a redacted one).
- Subsequent focused results reported for storage (63 passed), UI (114 passed),
  Rust (60 passed), and PTY (14 passed) supersede corresponding initial logged
  failures for those focused cases only. They do not prove the full suite passes.

The initial full-suite list also included `test_host_daemon_projects` and
`test_live_packaging`, both confirmed pre-existing baseline failures. No claim is
made that every original failure was fixed or independently rerun.

## 7. Tests

**No modes**
- A new Git session starts in the main checkout with no prompt.
- The removed config keys, the mode code and the old event handling are gone.
  Delete their tests rather than adapting them.
- New branches are `nexus/wt/<name>`.
- A non-Git workspace has no tools and no block.

**Prompt**
- A main root renders the guidance and facts.
- The facts follow the root after a move.

**Tools**
- `worktree_list`:
  - shows main, own and other sessions' worktrees, with merged/unmerged, dirty
    and the sessions in each;
  - marks the current row;
  - announces clipping.
- `worktree_create`:
  - from main, carries over owned changes;
  - from a merged worktree, branches from the updated base;
  - is refused at the limit.
- `worktree_move`:
  - to main, works;
  - to an own unmerged worktree, works;
  - to a removed checkout, recreates it;
  - to a worktree another session is in (active or not), works, and that
    session's root is left unchanged;
  - with dirty files, is refused;
  - with unmerged commits, is allowed with the warning.
- None of the three tools asks for permission.

**Merge permission**
- `git -C <main> merge --ff-only <nexus branch>` runs without asking in every
  permission mode.
- Any extra flag, other branch, other path, `--no-ff`, pipe or chained command
  falls back to the forced ask.

**Per-turn history**
- A turn in the main checkout records nothing: no `checkpoint.*` events and no
  refs.
- Turn diff and revert in the main checkout are refused with the reason.
- A turn in a worktree records a turn commit and its hidden ref.
- A turn that moves records only its worktree part.
- After a squash and merge, the old turns' diffs still work, and their revert is
  refused with the reason.
- The refs are bounded.

**Shared worktree**
- Two sessions in one worktree each commit only their own paths.
- A lost compare-and-swap race retries.
- A revert that would undo another session's later commit is refused.
- The facts name the other sessions.
- A fork stays in the parent's worktree.
- Cleanup waits until the last session leaves.

**In-turn move**
- The move applies only after every tool call in the step has finished.
- Sibling tool calls use the old root.
- The step count continues.
- The notice survives compaction.
- No steer or compaction lands between the request and the move.
- Turn diff across a move is refused.
- A vanished root recovers while idle.

**Replay.** main → wt1 → merge → main → wt2 (left unmerged) → main reproduces
the same `ConversationView` (root, worktree list, timeline rows) from records
alone.

**Cleanup**
- A merged, clean checkout is removed on move.
- Dirty and unmerged checkouts are kept.
- Branches always remain.

**TUI (native PTY check, as in plan 08)**
- No ask modal appears.
- `/worktree` is gone.
- The bar changes to `⎇ <name>` after an agent move, with no tab switch.
- `/worktrees` lists the worktrees and moves the session.

**Scripted provider end-to-end.** The agent lists, creates, edits, commits,
merges with Git, and moves to main, all within one turn.

## 8. Decisions and open questions

**Decided (2026-10-24):**
- No worktree modes and no up-front question. The agent decides.
- No enforcement: the guidance is advice only.
- Moves in the middle of a turn are allowed.
- The agent merges when done, or leaves the worktree unmerged; both are valid.
- The agent can list all worktrees.
- A merge the agent decides to do needs no approval (3.3).
- Worktrees are project-level and shared by any number of sessions; there is no
  session-to-worktree ownership (3.4).
- Main-checkout checkpoints are removed; per-turn history exists only in
  worktrees. Worktree turn commits are pinned by hidden refs so they survive a
  squash (3.5.1).
- Branches are `nexus/wt/<name>`.
- This is a breaking change: no backwards compatibility, no shims, no migration,
  and no support for older sessions (3.1).

**Remaining questions / verification work (latest full-suite run):**
- Latest full suite (`/tmp/full3.log`): 8,505 passed, 361 skipped, 4 failed,
  6 deselected in 814.12s. The plan remains not complete only because the overall
  suite has known unrelated failures; plan-related worktree-focused verification
  is green.
- The four failures are unrelated or stale test expectations:
  - `test_host_daemon_projects` has an environment-capture failure.
  - `test_hot_extension_e2e.py::test_config_system_files_and_skills_are_hot_next_iteration`
    and `test_manifest_loop_integration.py::test_reload_refreshes_config_soul_memory_skills_next_iteration`
    assert exact tool lists that omit the present `subagent_continue` tool.
  - `test_live_packaging` has a Rust pin mismatch.
- Focused worktree/session verification passed, including
  `test_worktree_sessions_runtime`, setup, project worktrees, cleanup host,
  tools, `host_facade`, and the Ratatui worktree bar. `test_phase3_exit` passed.
  Phase-6 worktree-child tests passed after their obsolete expectations were
  updated to the shared-root fork semantics specified in §3.4.
- `ruff check nexus tests` and `git diff --check` passed. Rust TUI tests passed:
  690 + 60 passed, 3 ignored.
- The historical `artifacts/backend-current.log` and
  `artifacts/backend-targeted4.log` results have been superseded by fixes and
  focused reruns; do not use their failure counts as current status. The earlier
  scripted-flow concern about a compound shell command is also resolved; there
  is no remaining plan-related worktree verification issue reported here.
- Run the full suite again after the unrelated failures are addressed before
  claiming overall verification complete.

