# Plan 07: Only root agents may spawn subagents

Status: implemented and verified in tests (2026-10-20). Unit and integration tests passed (.venv/bin/python -m pytest exact feature suite: 781 passed, including `tests/test_subagent_runtime_security.py`, `tests/test_subagent_runner.py`, `tests/test_session_tree.py`, `tests/test_codemode_*.py`; cargo test --locked rust/tui: 590 passed 2 ignored plus 58 dashboard passed). Child runtimes get no runner,
`ROOT_ONLY_TOOLS` is removed from child selection, `spawn` refuses at depth ≥ 1, the root runner clamps
`max_depth` to `min(value, 1)` (0 disables; the shared `SubagentBudget` is untouched), and host
`SessionTree` uses a fixed depth of 8 for legacy nested sessions. Source: root `TODO.md` item 4
("Any sub-agent like task quick or advisor should not be able to launch more
sub-agents. It should not have the sub-agent tool. Only root or lead-level agents
which are build and orchestrator can have it").

## 1. Intent

A subagent (`task`, `quick`, `advisor`, `scout`, or any user/project role running
in the `subagent` context) never has the `subagent` tool, so the agent tree is at
most one level deep: root → children. Root sessions (`build`, `orchestrator`, and
other roles running in the `root` context) keep it.

### Non-goals

- Changing what external workers (Claude Code, Antigravity) do inside their own
  process; Nexus only controls the tools it gives them.
- Changing the manager session (it already has no `task` bundle,
  `nexus/tools/bundles.py:194-196`).
- Rewriting stored sessions: nested children recorded before this change still
  replay and render.

## 2. Current evidence

- `subagent` lives in the `task` bundle with `todowrite` and `question`
  (`nexus/tools/bundles.py:127-130`); the `coding`, `coding_meta` and `research`
  profiles all include it (`bundles.py:175-191`).
- Child tools are `parent ∩ role ∩ requested` (`docs/agents.md:363`;
  `SubagentRunner.select_tools`, `nexus/agents/runner.py:1375-1410`, which calls
  `AgentManager.select_tools`, `nexus/agents/manager.py:754-818`). A role with no
  positive `bundles`/`tools` inherits the whole ceiling, so built-in children
  (no `bundles:` keys in `nexus/agents/data/{task,quick,advisor,scout}.md`) get
  `subagent` whenever the parent has it.
- Worktree children are additionally limited to `WORKTREE_CHILD_TOOLS`, which
  includes `subagent` (`runner.py:133-148`).
- Depth is bounded only by `agents.max_depth` (default 3,
  `nexus/config/schema.py:304`; `runner.py:124,1537-1543`).
- Roles declare `contexts: [root]` or `[subagent]`
  (`nexus/agents/data/*.md:4`); root roles are `build`, `orchestrator`.
- Existing tests exercise nested children, e.g.
  `tests/test_session_tree.py:124` (`test_nested_runtime_physical_owner_root_and_spawn_sequence`).
- Code mode calls tools through the normal pipeline restricted to the current
  catalog (`docs/tools.md`, code mode section), so removing `subagent` from a
  child's catalog also removes it from that child's `code` programs (inference;
  must be tested).

## 3. Design

Recommendation: **enforce structurally in the child tool selection, keyed on
context, not on role names.** Deciding trade-off: a name allowlist
(`build`, `orchestrator`) would silently strip user-defined root roles and keep
a hole for any custom role that runs as a child; "a child never gets `subagent`"
is one rule that covers built-ins, user roles and worktree children.

1. In `SubagentRunner.select_tools` (`runner.py:1375`), always remove
   `subagent` from `selection.selected` and add it to `dropped`/`stripped`, with a
   reason string such as `"subagents cannot spawn subagents"` surfaced in the
   existing dropped-tools report.
2. In the child catalog builder, stop adding `catalog_map["subagent"] =
   build_task_tool(runner)` for children (`nexus/runtime.py:6623-6626`; the
   nested runner is created at `runner.py:2050-2068`). Remove `subagent` from
   `WORKTREE_CHILD_TOOLS` (`runner.py:141`), which feeds the worktree filter at
   `nexus/runtime.py:6627-6634`.
3. Defence in depth: in the spawn path (`runner.py:1505-1545`) refuse a spawn
   when `self._depth >= 1` with an explicit error outcome, so a stale catalog or
   an extension that re-registers the tool cannot nest.
4. `agents.max_depth`: keep the key parseable for config compatibility, clamp
   the effective value to 1, and document that values above 1 are ignored
   (same treatment as legacy `agents.max_tier`, `docs/agents.md:366-368`).
5. Roles running at root keep current behaviour; a root role may still drop the
   tool with `tools: ["-subagent"]`.
6. Prompts: confirm `nexus/agents/data/{task,quick,advisor,scout}.md` never tell
   the child to delegate (today they do not); `orchestrator.md`/`build.md` stay.
7. UI: nothing new; existing nested agent pages for old sessions keep working
   (Ratatui keeps a parent stack, `nexus/ui/ratatui/workflows.py:117-118,541`).

## 4. Tests and docs

- `tests/test_subagent_runner.py`: each built-in child role (with and without
  worktree) has no `subagent` in its selection and reports it as dropped; a
  child whose role explicitly lists `subagent` still does not get it; a direct
  spawn attempt at depth 1 is refused; a root role keeps it.
- Code mode: a child's `code` program cannot call `subagent`
  (`tests/test_codemode_*.py`).
- Update tests that rely on nesting (`tests/test_session_tree.py:124` and any
  others found by searching for depth ≥ 2 spawns): keep replay/rendering of
  legacy nested logs covered with recorded fixtures rather than live spawns.
- Docs: `docs/agents.md` (bounds section: tools rule and depth), `docs/tools.md`
  (`subagent` availability), `docs/config.md` (`agents.max_depth` now clamps to 1),
  `docs/decisions.md` (why context-based, not name-based).

## 5. Acceptance criteria

- [x] In a root `build` or `orchestrator` session, `subagent` is available.
- [x] In every child session (any role, worktree or not), `subagent` is absent
      from the advertised tools, from code-mode helpers, and a forged call is
      refused.
- [x] `agents.max_depth = 3` in config loads without error and has no nesting
      effect.
- [x] Sessions with recorded nested children still open, replay and show their
      agent pages.
- [x] Offline tests pass (part of feature suite: 781 passed; cargo test 590 + 58 passed).

## 6. Risks and open decisions

- **Conflict to resolve with in-progress plan 01**
  ([01-unified-sessions-and-handoffs.md](../in-progress/01-unified-sessions-and-handoffs.md)):
  it still carries work for nested children (ownership trees, nested metadata,
  `docs/sessions.md:157-163`). After this plan, nested children only exist in
  legacy logs; plan 01 should narrow that work to replay/read-only support.
- Open: should a user be able to opt a custom role back into nesting? Default:
  no; reversing needs an explicit decision recorded in `docs/decisions.md`.
- Behaviour change for anyone relying on `orchestrator` → `task` → `quick`
  chains; the orchestrator must now fan out directly. Check `orchestrator.md`
  guidance still reads correctly.
- Interaction with [plan 06](06-subagent-roster-and-settings-notice.md): the
  roster and change notice then only matter in root sessions.

## 7. Verification ledger

- Tests independently verified by parent: `.venv/bin/python -m pytest` (exact feature suite: 781 passed, including `tests/test_subagent_runtime_security.py`, `tests/test_subagent_runner.py`, `tests/test_session_tree.py`, `tests/test_codemode_*.py`) and `cargo test --manifest-path rust/tui/Cargo.toml --locked` (590 passed, 2 ignored + 58 dashboard passed).
- Verification commands: `.venv/bin/python -m pytest tests/test_subagent_runtime_security.py tests/test_subagent_runner.py tests/test_session_tree.py`
- Limitations: Spawning is strictly limited to depth 1 (root agents only); nested spawning is blocked permanently. Legacy sessions with recorded nested agents remain supported for replay and read-only inspection up to depth 8. External workers (Claude Code, Antigravity) are not restricted by internal Nexus tool filtering.
