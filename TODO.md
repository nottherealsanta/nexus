1. new skill add ( with npx skills ) ( project level or global level )
2. new MCP add ( project level or global level )
3. Keep the `subagent` tool definition stable when tier model lists change.
   Look up the live roster with a tool call instead of baking it into the schema.

   Intent
   The general rule is to never do anything that invalidates the prompt cache,
   and a tool definition that changes whenever the tier models change breaks
   that rule. Spawning a subagent never needs to know which model sits in a
   tier. It needs only the tier: "run this as a low task", "give me a high
   advisor". Which `provider/model` serves that tier is a backend concern
   handled by the router and the per-turn runner. So the `subagent` tool
   definition stays fixed: the agent asks for a tier by name, and the
   description never lists model ids. The concrete ids matter only when the
   user asks for a specific model. For that case the same tool takes a boolean,
   `get_tiers_models_info`. When it is true, the tool spawns nothing and returns
   the current list of model ids in each tier, per role, as an ordinary tool
   result. That result lands in history instead of the cached system+tools
   prefix, so the cache survives. It is also always current: if the tiers have
   changed since the last lookup, the agent calls the tool again with the flag
   on and gets the updated list, then passes the model it wants on a normal
   spawn call. The definition never has to change, and the agent is never told
   it did.

   Problem
   - Changing `models.tiers` (or a role's tier list) does not rewrite, compact, or
     otherwise touch conversation history. The system prompt does not contain the
     roster either. `Runtime.reload_tier_config` only rebuilds the tier table and
     the router.
   - It does change the `subagent` tool definition. `build_task_tool`
     (`nexus/tools/builtin/task.py`) builds the description with `_describe`,
     which calls `SubagentRunner.role_index()` (`nexus/agents/runner.py`). That
     text lists every role, the tiers it may use (first is the default), and the
     `provider/model` ids in order, plus the "Choosing a tier" guidance. The JSON
     parameter schema stays the same; the description does not.
   - The next turn builds a fresh `SubagentRunner` and calls `build_task_tool`,
     so the new description is what the model sees. The prompt-cache breakpoint
     `system_tools` sits on the stable system+tools prefix (Anthropic puts
     `cache_control` on the last tool definition). A real roster change makes
     that prefix miss and writes a new cache entry. An edit that does not change
     the rendered roster text still hits. Other tool schemas are unaffected.
   - A one-line context note such as "tool definitions have changed" is not the
     fix. That note is still new prefix bytes, so it invalidates the same cache,
     and it still tells the model the tool definition moved.

   Decision
   - The tool definition must not change when tiers or their model lists change.
     Tier assignment stays a backend concern (router, per-turn runner). The model
     learns the current roster from a tool result, which is ordinary history and
     does not move the system+tools prefix.
   - Do not add a context part, system-prompt line, or description edit to
     announce that the roster changed.

   Behavior
   - Remove the live roster from the `subagent` description. The description and
     the JSON schema are fixed strings. They must not call `role_index()` or
     interpolate tier or model ids.
   - Keep a short, stable instruction in the description: tiers and concrete
     `provider/model` ids are not listed here; before choosing a tier or a
     specific model, call `subagent` with `get_tiers_models_info` true and no
     prompt. Leave the rest of the spawn instructions as they are.
   - Add a boolean parameter `get_tiers_models_info` (default false). It has to
     be a declared property: the tool schema is `additionalProperties: false`,
     so an undeclared flag is rejected before `run`.
   - `prompt` is required only for a spawn. When `get_tiers_models_info` is
     true, `prompt`, `description`, `subagent_type`, `tools`, `model`, and
     `worktree` are ignored and must not be required. The handler returns before
     `_build_request` spawns.
   - The lookup does not spawn a child, open a session, or create a worktree.
     It reads the live runner (`role_index()` or the same data) and returns a
     normal tool result, model-visible, not a new context part. Include each
     agent, the tiers it may use, and the `provider/model` ids in order, plus
     the existing guidance: omit `model` for the default tier; pass `low`,
     `medium`, or `high` only when the role lists it; a tier or model outside
     the role's list runs on the nearest allowed tier; if a route fails, call
     the next model in that tier and do not repeat a failed route. Keep the
     `MAX_ROLE_INDEX_CHARS` cap and the truncation note.
   - When the flag is false or omitted, spawning is unchanged.
   - After the user asks for a specific model, the agent calls this lookup,
     then passes `model` on a normal `subagent` call. The backend still clamps
     to the role's list.

   Docs and tests
   - `docs/tools.md` subagent row currently says the description lists each
     role's tiers. Point that at the lookup call.
   - `docs/agents.md` tiers section says the same thing about the tool
     description. Describe the lookup result instead.
   - `docs/decisions.md`: record why the roster left the tool definition (a
     description change invalidates the `system_tools` prefix; a context note
     would too).
   - `docs/context.md` only if it claims tool definitions track tiers. The
     prefix rule itself does not change.
   - Update `tests/test_builtin_task.py` (`test_task_tool_describes_tier_choice`
     and any description assertion), `tests/test_subagent_tiers.py`, and
     `tests/test_subagent_runner.py`. Assert the schema text is identical for
     two different tier tables, that `get_tiers_models_info: true` returns the
     roster and does not spawn, and that a spawn still requires `prompt`.

