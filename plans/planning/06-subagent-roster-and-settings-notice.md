# Plan 06: Model names and effort in the `subagent` description; settings-change history notice

Status: implemented and verified in test suite (2026-10-20). Unit and integration tests passed (.venv/bin/python -m pytest exact feature suite: 781 passed, including `tests/test_subagent_roster_notice.py`, `tests/test_subagent_tiers.py`, `tests/test_subagent_runner.py`). Cache-miss prefix byte stability is verified offline; live provider cache token metrics remain unverified. Implementation corrections to the text below: the notice is driven by
the request boundary (digests carried on the dispatcher, noted by the loop before
assembly), not by `reload_tier_config`; old notices are never stripped (stripping
rewrites the cached prefix) and age out through ordinary compaction; digests are
split into models and efforts and stored in `context.notice` events (baseline =
event only); efforts use the shared `effective_tier_effort` rule and the router's
live efforts; role-level `reasoning_effort` is now shown in the role line but not
tracked. See `docs/context.md#subagent-roster-notice-plan-06` and
`docs/decisions.md`. Source: root `TODO.md` item 3. Sections 1-6 below are that item's text, moved here unchanged
in substance; section 0 records what was checked on 2026-10-20.

## 0. Current evidence (checked 2026-10-20)

- `SubagentRunner.role_index()` (`nexus/agents/runner.py:1179-1202`) renders one
  `<tier> models (in order): ref (effort: x), ...` line per tier from
  `models.tier_efforts`, then the role index (`AgentIndex.render`), joined by a
  blank line.
- The over-budget path is still a raw character slice:
  `rendered[:max(0, max_chars - len(marker))] + marker` (`runner.py:1199-1201`),
  so it can split a line and drop the role index. The constant comment already
  says "whole lines only" (`runner.py:130-131`, `MAX_ROLE_INDEX_CHARS = 4_000`);
  the code does not honour it yet.
- `build_task_tool`/`_describe` add the roster under "Available agents
  (subagent_type)" and tolerate a failing roster (`nexus/tools/builtin/task.py:136-173`).
- `Runtime.reload_tier_config` rebuilds the tier table, re-tiers the registry
  and updates the router and assembler; it emits nothing to history
  (`nexus/runtime.py:6917-6935`). Tier edits now apply live
  (`docs/decisions.md:931-942`), which notes the roster's effort labels in an
  already-running turn are not verified.
- No history-notice mechanism exists (no matches for a settings-change notice
  in `nexus/`).
- Tests: the test named in the TODO, `test_task_tool_describes_tier_choice`, does
  not exist in `tests/test_builtin_task.py`; the matching tests today are
  `tests/test_subagent_tiers.py:187,202,214` (`test_roster_shows_tiers_and_the_default`,
  `test_tool_description_teaches_tier_choice_with_the_roster`,
  `test_roster_budget_keeps_every_builtin_role`) and
  `tests/test_subagent_runner.py:1233`
  (`test_tool_roster_lists_all_tier_models_and_failure_instruction`).
- Interaction with [plan 07](07-root-only-subagent-spawning.md): once only root
  agents have the `subagent` tool, the roster and the notice matter for root
  sessions only.

## 1. Intent

The `subagent` tool description itself lists the configured tiers, the
`provider/model` ids in order for each tier, and the effort per model, plus
the spawnable roles. There is no separate parameter or lookup call for roster
info. When `models.tiers` or tier effort settings change, the rendered
description changes and the `system_tools` prefix misses the cache once. That
miss is accepted. The change notice must not add a second one: it is appended
at the tail of persisted history (after the cached prefix), never into the
system prompt or tool definitions. The notice only says which setting was
updated and defers to the current tool description. It does not repeat the
roster, model ids, or effort values.

## 2. Problem

- Changing `models.tiers` does not rewrite, compact, or otherwise touch
  conversation history. The system prompt does not contain the roster either.
  `Runtime.reload_tier_config` only rebuilds the tier table and the router.
- It does change the `subagent` tool definition. `build_task_tool`
  (`nexus/tools/builtin/task.py`) builds the description with `_describe`,
  which calls `SubagentRunner.role_index()` (`nexus/agents/runner.py`). Today
  `role_index()` already renders one line per tier,
  `<tier> models (in order): ref (effort: x), ...`, using
  `models.tier_efforts`, followed by the role name/description index, and the
  description carries the "Choosing a tier" guidance. The JSON parameter
  schema stays the same; the description does not.
- Two gaps in `role_index()` today: the output is cut with a raw character
  slice, which can split a line and drop the role index entirely when the
  tier lines are long; and the layout is per tier, not per role.
- The next turn builds a fresh `SubagentRunner` and calls `build_task_tool`,
  so the new description is what the model sees. The prompt-cache breakpoint
  `system_tools` sits on the stable system+tools prefix (Anthropic puts
  `cache_control` on the last tool definition). A real roster change makes
  that prefix miss and writes a new cache entry. An edit that does not change
  the rendered roster text still hits. Other tool schemas are unaffected.
- A change notice placed in the system prompt or tool definitions would be
  new prefix bytes and would add a second `system_tools` miss. The notice
  must go at the tail of history, where it costs no extra miss.

## 3. Decision

- The roster (tiers, `provider/model` ids, tier effort) stays in the
  `subagent` description. Do not add a `get_tiers_models_info` parameter or
  any other lookup call; the JSON parameter schema stays as it is.
- Effort shown is the per-model tier effort (`models.tier_efforts`) that
  `role_index()` already renders. A role-level `reasoning_effort` override
  (which wins at spawn) is not additionally listed or tracked; it is out of
  scope here. Reverse this only by an explicit decision.
- When the relevant settings change, append a short notice to persisted
  history at the tail. It names the updated setting (for example "subagent
  tier models" or "subagent effort") and tells the model to use the current
  `subagent` tool description. It must not repeat the roster.

## 4. Behavior

- Keep `_describe` / `role_index()` rendering the roster in the description.
  Keep the existing guidance and the `MAX_ROLE_INDEX_CHARS` cap, but truncate
  on whole lines only, never mid-line, and keep the role index visible: the
  tier lines are budgeted so at least the role names remain, and the clip
  note is appended on its own line.
- Change detection compares a digest of the rendered tier/model/effort lines
  (not the role descriptions, so a role-description edit gives no notice),
  not raw setting edits. An edit that does not change those lines produces
  no notice and no cache miss.
- The last-notified digest must be durable state tied to the session, so a
  reload or resume compares against it and does not emit again. Where it
  lives (session metadata, a history entry field, etc.) is an
  implementation choice.
- Emit the notice once per change, after `Runtime.reload_tier_config`
  applies it, as a persisted history entry appended after existing messages.
  Do not rewrite, compact, or reorder earlier history.
- Design requirement (no history-notice mechanism exists yet): decide the
  entry's representation before implementing. It must keep provider
  user/assistant alternation valid (for example, a user-role message
  merged into or placed after the final user turn, never two consecutive
  same-role messages or a dangling tool call), and it must be handled by
  compaction (either dropped as stale or kept only if still current).
  Record the choice in `docs/context.md`.
- Spawning is unchanged.

## 5. Implementation steps

1. Split `role_index()` into `tier_lines()` and the role index; budget tier
   lines first against `max_chars` minus the role names' minimum, drop whole
   tier lines from the end, then append `[Subagent roster and tier models clipped]`
   on its own line.
2. Add `roster_digest()` = SHA-256 of the joined tier lines only.
3. Decide and document the notice representation (section 4 requirement)
   in `docs/context.md`, then implement it in the session layer, storing the
   last-notified digest durably with the session.
4. On the turn after `reload_tier_config` (or at turn start when the digest
   differs from the stored one), append one notice; never at system/tool level.
5. Compaction: drop notices that are no longer current.

## 6. Docs and tests

- `docs/tools.md` and `docs/agents.md`: state that the description lists
  each tier's models and effort and the spawnable roles, and that a settings
  change appends a history notice.
- `docs/decisions.md`: record the trade-off (a roster change in the
  description costs one `system_tools` miss; the notice goes at the history
  tail so it adds none, and it defers to the description instead of
  repeating it).
- `docs/context.md`: document the notice as a tail-of-history entry.
- Update `tests/test_builtin_task.py` (any description assertion),
  `tests/test_subagent_tiers.py` (including
  `test_tool_description_teaches_tier_choice_with_the_roster`, which the TODO
  called `test_task_tool_describes_tier_choice`), and
  `tests/test_subagent_runner.py`. Assert the description includes model ids
  and effort, that it changes when the tier table changes, that a tier
  change appends one tail notice that names the setting and omits model ids,
  that an edit with identical rendered text appends nothing, that the notice
  is not duplicated after reload/resume, that truncation keeps whole lines
  and the role index, and that alternation stays valid.

## 7. Acceptance criteria

- [x] Over-budget rosters end on a whole line, list every role name, and carry
      the clip note on its own line.
- [x] A tier/model/effort change appends exactly one tail notice naming the
      setting, with no model ids or effort values; nothing changes in the
      system prompt or other tool schemas.
- [x] Identical rendered tier lines → no notice; reload/resume → no duplicate.
- [x] Provider alternation stays valid for Anthropic and OpenAI request shapes;
      compaction drops stale notices.

## 8. Risks and open decisions

- Open (must be decided first): the notice's message representation and where
  the digest lives.
- Running turns keep the runner's construction-time config
  (`docs/decisions.md:941-942`); the notice must be emitted against the config
  the next request will actually advertise, not an earlier snapshot.
- Prompt-cache claims (one miss, no second miss) need a recorded provider-usage
  check; offline tests can only assert byte stability of the prefix.

## 9. Verification ledger

- Tests independently verified by parent: `.venv/bin/python -m pytest` (exact feature suite: 781 passed, including `tests/test_subagent_roster_notice.py`, `tests/test_subagent_tiers.py`, `tests/test_subagent_runner.py`).
- Verification commands: `.venv/bin/python -m pytest tests/test_subagent_roster_notice.py tests/test_subagent_tiers.py tests/test_subagent_runner.py`
- Limitations: Prompt-cache claims (one miss, no second miss) verified for byte stability of prefix offline; live provider cache token metrics remain unverified against live endpoints. Role-level `reasoning_effort` override is displayed in the role description line, but edits to role reasoning effort are not tracked in the notice digest.
