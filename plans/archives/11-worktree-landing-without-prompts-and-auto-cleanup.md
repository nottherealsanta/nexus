# Plan 11: Landing a worktree without a permission prompt, and removing the checkout when done

Status: implemented and offline verified; acceptance remains partial (2026-10-26). What the code already does
(tested): §4.4 automatic return — a clean landed worktree session returns to
main at turn close and before archive (`SessionWorktrees._return_landed` /
`archive_landed`, `remove_after_landing` user-only key defaulting to true;
`docs/sessions.md` "Automatic return after landing", `docs/config.md` "Landed
worktree lifecycle"; tests `test_landed_turn_returns_to_main_without_request`,
`test_archive_returns_landed_checkout`, `test_remove_after_landing_is_user_only`).
This change implements on top of that: §4.1 evidence + §4.2 parser widening —
`is_worktree_landing` now also accepts the told `&&` chains of landing commands
(checkout-then-merge, at most three `-C` segments, or `cd <main>` plus up to two
bare-`git` landings), the `cd <main> && git …` spellings, `~`-expanded mains,
`--` before the branch and `--ff-only` after it; every near-miss (extra segment,
`;`/`||`/pipe/redirect, wrong path or branch, `-c`/env prefix) keeps the forced
ask, and `deny` rules still win first (probed 2026-10-26 against the running
code, e.g. chained/`cd`/reordered shapes returned `False` before this change and
`True` after; near-misses stay `False`). Instructions now run the told landing
one command per shell call (`parts.py`, `worktree_bar.py`); the stale
"always approves" paragraph is fixed (`docs/decisions.md`), with the widened
exemption stated in `docs/tools.md`/`docs/security.md`. Not in this change: §4.3
`/worktrees` Integrate confirm (Q1 unanswered — default no), anything that
weakens the forced ask for arbitrary Git writes elsewhere. Remaining unverified:
live end-to-end landings issued by the agent in a real checkout, and real-agent
telemetry of which new shape fires in practice. Source: root `TODO.md`
items 3 and 4, quoted unchanged:

> - worktree merge should not ask for permission to merge
> - Auto delete worktree when done with it

Planned together: "done with it" is, in practice, "landed", so cleanup hangs off the same
landing detection.

## 1. Intent

1. When the agent lands its own session worktree the way Nexus tells it to, the user is not asked
   for permission.
2. Once a worktree's work is landed and nothing else needs the checkout, the checkout is removed
   automatically and safely (the branch and record stay, so it can be recreated).

### Non-goals

- No bespoke merge command (removed on purpose, `docs/decisions.md:63-68`).
- Never weaken the forced ask for arbitrary Git writes elsewhere (`docs/decisions.md:75-81`,
  `nexus/tools/permissions.py:1223-1231`); widen only the parsed landing exemption.
- Never remove a dirty checkout, a checkout another session is in, or an unmerged one under default
  policy (`docs/sessions.md:111-121`, `docs/config.md:91-99`).
- Subagent worktrees stay disabled (`docs/decisions.md:96-106`).

## 2. Current state (checked 2026-10-25)

### Landing permission

- **The canonical landing already never asks.** `PermissionEngine` allows
  `git -C <main> merge --ff-only <worktree branch>` and `git -C <main> checkout|switch <base>` in
  every mode (deny rules still win) via `is_worktree_landing`
  (`nexus/tools/permissions.py:1268-1309`, check at `:1513-1520`), with the policy built from the
  workspace path, its resolved path, and the controller's branch predicates
  (`nexus/runtime.py:4847-4854`, installed at `:5470`). Tests:
  `tests/test_worktree_merge_permission.py`. Documented in `docs/tools.md:96-108`.
- `docs/decisions.md:75-84` still says "The user always approves" — **stale** relative to the code
  and `docs/tools.md:104-105`. Fix that paragraph in the implementing change.
- Anything else that writes Git in another location is a forced ask: `-C`/`--git-dir`/`--work-tree`
  options or a `cd` before a Git write (`_is_git_write_elsewhere`, `permissions.py:1312-1317`,
  regexes `:1232-1248`).
- The parser rejects (keeps the ask) any of: `&&`/`;`/pipes/redirects (`_LANDING_PUNCTUATION`,
  `:1265`, `:1293`), `$`, backticks, backslashes, newlines (`:1284`), `cd <main> && git merge …`,
  extra flags or a different flag order (`merge <branch> --ff-only`), `--ff-only` combined with
  anything, and paths that `os.path.normpath` does not map onto `policy.mains` (e.g. `~/…`, a
  trailing `/.`, a symlinked alias other than the resolved path).
- The agent's instructions name the exact form (`nexus/context/parts.py:623`, `:664`;
  `nexus/ui/ratatui/worktree_bar.py:114` for `/merge`).
- The user-initiated `/worktrees` → **Integrate into base branch…** path is a separate, two-step
  host flow with a confirmation token (`nexus/ui/ratatui/worktree_pages.py:119-131`,
  `nexus/session_integration.py:1-8`).

So the remaining prompts the user sees must come from command shapes outside the parser, or from
the `/worktrees` confirm step. **Which one is not known yet** (step 1 below finds out).

### Cleanup

- Automatic cleanup exists only on **move away**: when the last session leaves a worktree,
  `_cleanup_left` → `worktree_cleanup.auto_cleanup` removes a merged, clean checkout and emits
  `worktree.removed` (`nexus/worktree_sessions.py:1301-1307`, `nexus/worktree_cleanup.py:195-236`;
  caller `worktree_sessions.py:1248`).
- Landing is detected at every step boundary and at turn end and emits `worktree.merged` plus a
  "Merged into …" notice (`worktree_sessions.py:552-571`, `_landing` `:1311-1357`).
- After a landing the session **stays in the worktree**, so the checkout is never "left" and never
  cleaned up. That is the gap behind item 4.
- Removal keeps branch and record (`worktree_cleanup.py:1-16`, `:230-234`); `recreate_worktree`
  rebuilds the checkout (`docs/sessions.md:98-104`).

## 3. Assumptions

- A1. "merge" in item 3 means the agent's own landing (the documented path). The `/worktrees`
  Integrate confirm step is covered as a secondary change in §4.3 and is gated on Q1.
- A2. "done with it" means: the worktree's head is landed (`worktree.merged` for the current head),
  the checkout is clean, no other session is in it, no background job runs, and the session's turn
  has ended with nothing queued.

## 4. Design

### 4.1 Find the prompts that still happen (evidence first)

Export a few real sessions that landed (`nexus` session export, `docs/sessions.md`) and list every
`permission.requested` for the `shell` tool whose command contains `merge`, `checkout`, `switch`
or `-C`. Classify each by why `is_worktree_landing` refused it. Record the table in this plan
before changing the parser. Expected (inferred, not observed) classes: chained
`checkout && merge`, `cd <main> && git merge --ff-only …`, flag order, `2>&1`, `~` paths.

### 4.2 Widen the landing exemption narrowly (`nexus/tools/permissions.py`)

Accept, still as parsed tokens with `posix=True` shlex and the same rejection of `$`, backticks,
backslashes and newlines:

1. A `&&` chain of 1–3 segments in which **every** segment is itself an accepted landing
   (`checkout|switch <base>` then `merge --ff-only <branch>`). Any `;`, `||`, pipe, redirect or
   other segment → not a landing.
2. `cd <main> && git merge --ff-only <branch>` (and the checkout variant), where `<main>` passes the
   same `policy.mains` check.
3. `merge --ff-only <branch>` with `--ff-only` after the branch.
4. Directory matching: compare `os.path.realpath(os.path.expanduser(dir))` as well as `normpath`
   against `policy.mains` (`~` is expanded by the shell, so accepting it is truthful).

Only add a class that §4.1 actually observed. Each accepted shape gets a positive test and a
near-miss negative test (extra flag, extra segment, wrong path, unknown branch, `;` instead of `&&`)
in `tests/test_worktree_merge_permission.py`.

Also tighten the instructions so the agent emits the canonical single command: one command per
shell call, no chaining (`nexus/context/parts.py:660-670`, `worktree_bar.py:114`).

### 4.3 `/worktrees` Integrate (only if Q1 says so)

Skip the second "Confirm" menu when the host preview reports a pure fast-forward with no dirty
overlap; keep it for anything else. The host keeps the token check: the client would call
`integrate_session` once to get the token and immediately again with it, so the safety check
(state bound to the token) is unchanged and only the extra click goes away. Change lives in
`nexus/ui/ratatui/worktree_pages.py:125-131`; no host change.

### 4.4 Remove the checkout after landing (`nexus/worktree_sessions.py`)

At turn end (`finish` → `_close_part` → `_detect_landing`, `:539-571`), after a `worktree.merged`
for the **current head**:

1. Re-check A2 in the host (not the client): `merge_state(record)["merged"]`, clean status, no
   other occupant (`_others_in`), no active jobs (`runtime.session_has_active_jobs`), empty queue.
2. Move the session's root back to the main checkout using the existing move path (the same code
   `worktree_move` to main uses), with reason `landed`, so the agent gets the ordinary durable
   move notice (`_move_text`, `:1275-1299`: "Working directory is now the main checkout … its work
   is merged").
3. The move already calls `_cleanup_left` (`:1248`), which runs `auto_cleanup` with all its
   refusals and emits `worktree.removed` with a reason.
4. Policy key: `[worktrees] remove_after_landing` (boolean, **user-level only** like
   `auto_remove_clean`, default `true`; `nexus/config/schema.py` + `docs/config.md`). Default on
   because it only removes a clean, merged, recreatable checkout, which is the existing default
   cleanup policy.

Also run the same check on **archive** of a session whose root is a landed worktree (the session
leaves for good). Find the archive path in `nexus/session/manager.py` and call the controller from
the runtime layer, not from the session layer (one-way layering, `AGENTS.md:82-85`).

### Host, events, persistence

- No new event types: `session.root_set`, `context.notice`, `worktree.removed` already exist and
  replay through the reducer (`view.worktree.moves`, `prototype.py:1234-1251`).
- No schema change. A config key is added (validated, user-level only).
- The TUI shows the existing "Worktree" move row and a removal row; nothing UI-only.

## 5. Implementation order

1. §4.1 evidence table (no code).
2. §4.2 parser + instructions + `docs/decisions.md:75-84` fix + `docs/tools.md`/`docs/security.md` wording.
3. §4.4 post-landing move + cleanup + config key + docs (`docs/sessions.md:108-121`, `docs/config.md:91-99`).
4. §4.3 only after Q1.

## 6. Tests and acceptance

- `tests/test_worktree_merge_permission.py`: new accepted shapes and near-miss refusals; `deny` rule
  still wins; `mode="deny"` behaviour unchanged.
- A worktree-session test (next to the existing `_cleanup_left`/landing tests; find them with
  `rg "_landing|auto_cleanup" tests`): landed + clean + alone → root moves to main, checkout gone,
  branch and record kept, `worktree.removed` recorded; dirty / other occupant / running job / queued
  input / unmerged → kept with a reason; `remove_after_landing = false` → kept; project config
  setting the key → rejected.
- Replay test: the reduced view after replay shows the same move and removal rows.
- Acceptance:
  - [ ] Every landing shape seen in §4.1 runs without a prompt; arbitrary `git -C … reset` still asks.
  - [ ] After a landed turn the checkout disappears and the agent is told it is in the main checkout.
  - [ ] Nothing dirty or shared is ever removed; the branch can be recreated.
  - [ ] `docs/decisions.md` no longer says the user always approves the landing.

## 7. Risks and open questions

- Q1. Should the user's own `/worktrees` Integrate also skip its confirm? (It is a click the user
  makes, not an agent action.)
- Q2. Should the merged **branch** also be deleted? Not planned: branch and record are the recovery
  path and are kept by design.
- Q3. After the auto-move, a follow-up request in the same session runs in the main checkout. If
  the user expects every request to get a worktree, the "worktree at first message" rework
  (`docs/decisions.md:98-100`) must decide whether a later message creates a new one.
- Risk: a `&&` chain broadens the parsed surface; keep segments ≤ 3 and require every segment to be
  an accepted landing.
- Risk: moving the root at turn end races a user message that arrives during `finish`; re-check the
  queue under the session lock and skip the move if anything is pending.
- Not verified: unattended daemons with `on_unattended = "allow"` (`docs/decisions.md:82-84`).

### Offline verification follow-up

Permission parser, worktree runtime, cleanup-host, shared-session and replay/view
suites: 95 passed (42.16 s). This is offline evidence only; no real-agent session
export or live prompt telemetry was collected. No allowlist or permission policy
was widened in this follow-up. Live evidence gaps above remain open.

### Finisher acceptance audit (offline versus live)

Focused permission/runtime/cleanup/shared-session/view plus layering run: 337 passed.
Criterion mapping:
- canonical landing and explicit deny precedence -> test_worktree_merge_permission.py
  (including test_explicit_deny_rule_still_wins_over_the_landing), passed;
- full create/edit/merge/move turn -> test_worktree_sessions_runtime.py::
  test_full_agent_flow_list_create_edit_merge_and_move_to_main, passed;
- landed turn returns to main with feature toggle -> same file::
  test_landed_turn_returns_to_main_without_request, passed;
- archive returns landed checkout -> same file::test_archive_returns_landed_checkout, passed;
- only last session releases shared checkout -> test_worktree_shared_sessions.py::
  test_merged_clean_worktree_is_cleaned_up_only_when_the_last_session_leaves, passed;
- refusal on ignored files and stale preview -> test_worktree_cleanup_host.py::
  test_cleanup_refuses_ignored_files_and_stale_preview, passed;
- remove retains branch and next turn recreates -> same file::
  test_host_remove_keeps_branch_and_next_turn_recreates_checkout, passed;
- worktree removal reduction -> test_view_worktree.py, passed.
Live permission-request export/classification and real-session landing telemetry are absent.
The explicit divergent/dirty/running/queued individual acceptance mapping is not yet
complete; do not infer this audit proved all cleanup gates. Remains in planning.

### Offline gate continuation

Added real-repository parameterized `_return_landed` acceptance for dirty tracked
files, queued input, active jobs, and a new unlanded commit after an earlier merge.
All four pass and retain checkout/root with no removal event. Broader permission,
runtime, cleanup, shared-session, reduction and sidebar/native run: 162 passed,
1 native capture failure (subsequently fixed by explicit PTY resize notification).
Integrate confirmation remains intentionally unchanged: it authorizes a separate
user-selected integration preview, while the exemption is for the agent's own
parsed fast-forward landing. Advisor question if changing this scope is desired:
Should a user-selected clean fast-forward Integrate consume its preview token
without a second confirmation? Current safe default is no. Live telemetry absent.

Resolved scope decision: retain the explicit user Integrate preview confirmation.
The agent landing exemption is distinct; §4.3 is not selected. See
`docs/decisions.md`, "Agent landing versus explicit Integrate confirmation".
