# Agents, subagents and worktrees

`nexus/agents/` (L3) discovers agent definitions, runs bounded nested subagents
through the `subagent` tool, and manages isolated Git worktrees for children.
The concrete child runtime is built by `Runtime` (`_ChildRuntime`); the runner
never imports it.

## Files

| File | Owns |
| --- | --- |
| `model.py` | restricted frontmatter grammar, immutable `AgentDef`, limits, read-only role rules |
| `manager.py` | `AgentManager`: deterministic discovery, precedence, seeding, tool selection, legacy aliases |
| `runner.py` | `SubagentRunner`, `ChildSpec`, `SubagentBudget`: bounding rules and event relay |
| `worktrees.py` | `WorktreeService`: create, finalize, record, discard |
| `worktree_review.py` | immutable, read-only review snapshots |
| `worktree_integrate.py` | apply an acknowledged review to a clean parent, with a rollback journal |
| `data/*.md` | packaged roles |

## Definitions

One `.md` per agent: a restricted frontmatter block then the system prompt. There
is no YAML library; the grammar rejects anchors, aliases, tags, block scalars,
comments, nesting, duplicate and unknown keys. One bad line rejects the file.

Keys: `name`\*, `description`\*, `bundles`, `tools` (a leading `-` excludes),
`model` (opaque here: a tier, `inherit`, `provider/model` or bare id),
`provider`, `reasoning_effort` (`minimal` … `xhigh`), `fallback` (≤ 8 models,
tried before `models.fallback`), `tiers` (≤ 8 tier names the role may run on, the
first is its default; see [Tiers per role](#tiers-per-role)), `color` (`#RRGGBB`, else derived from the name),
`max_iterations`, `context_tokens`, `contexts` (`root`, `subagent`; default
subagent-only), `profile`, `write_roots` (`global`, `project`: narrows mutating
file tools to `~/.nexus/` and `<workspace>/.agents/`, never widens; credentials,
sessions, cache, trash and daemon files stay blocked).

**Packaged roles:** roots `build` (default) and `orchestrator`; subagents
`advisor` (read-only), `task`, `quick`. They are global, not copied into
workspaces; editing one in Settings writes an override to `~/.nexus/agents/`.
Settings presents one shared Agents page, with no global/project selector; model,
effort, fallback and prompt edits are saved to the user override. File-based
workspace definitions still follow the discovery precedence below.
`agents.seed_roles` opts a workspace into seeding `.agents/agents/` (once per
marker). Legacy names: `general` → `task` (`build` as a root); `explore`,
`plan`, `planner` → read-only `advisor`.

The packaged root prompts contain only the coding-assistant identity, Nexus
harness context and tool availability; orchestrator additionally states that it
works by orchestrating subagents.

**Precedence** (low → high): packaged < `~/.nexus/agents/` < `<workspace>/.nexus/agents/`
(legacy) < `<workspace>/.agents/agents/`. Lookups are case-insensitive;
deleting an override resurfaces the lower one. Every skip, shadow, collision and
forbidden declaration is kept as a diagnostic, never silently dropped.

**Declarations are not grants.** `bundles`/`tools` only narrow. The read-only
roles (`advisor` and legacy aliases) are structurally denied shell and
mutating-filesystem tools whatever their file says.

**Root agent choice** is per session (`AgentSelect`, `AgentReset`, `/agent`) and
applies from the next turn; locked after the first turn for prompt-cache reasons
([sessions.md](sessions.md)). `AgentDefaultSet` stores the default in global or
project config. The context sees only `name: description` lines; bodies are
snapshot per generation and disclosed on demand.

**Remembered model per root agent.** Picking a model or effort records it for the
root agent in effect (`~/.nexus/agent_models.json`, machine state, not config).
Selecting that agent in any session, including `AgentReset`, restores the model
and effort; a model that no longer resolves is skipped and the current one kept.
A brand-new session that never selects an agent does not restore (not done).

## Spawning (`subagent` tool)

`subagent(prompt, subagent_type?, tools?, model?, description?, worktree?)` runs a
named role or an ad-hoc agent. The child does not see the parent conversation:
the prompt is the whole assignment.

An omitted, null, or blank `model` override uses the role's configured model (or
its first tier, see below), falling back to the parent's model. Surrounding whitespace is trimmed from model
overrides; whitespace inside a model reference is rejected. Permission-key
resolution and child execution use the same normalization.

Four bounds are computed **before** the child exists, so a child can never gain
authority its parent lacks:

1. **Tools intersect:** `parent ∩ role ∩ requested`; dropped names are reported back.
2. **Permissions inherit:** the parent's snapshot and session grants pass through
   unchanged; `deny` stays absolute.
3. **Tier is bounded** by the role's `tiers` (when it declares them) and capped at
   `agents.max_tier` (default `high`); either move emits `agent.clamped` with a
   `reason` (`role` or `max_tier`). A role with **no** `tiers` keeps the older
   rule: a bare tier hint labels the spawn but does not replace the role's model,
   and with no role model the child inherits the parent's. Saving an agent
   definition applies to the next child call, even mid-turn.
4. **Budgets:** a shared `SubagentBudget` bounds depth (`max_depth` 3), concurrency
   (`max_concurrent` 4), fan-out (`max_fanout` 16) and aggregate tokens/cost
   (`token_budget`, `cost_budget`) across the whole tree.

### Tiers per role

A role lists the tiers it may use: `tiers: [low, medium]`. The first is the
default. The built-ins are `quick` `[low]`, `task` `[low, medium]` and `advisor`
`[medium, high]`; roots (`build`, `orchestrator`) have none. A user-added role
works the same way and new roles from Settings start with `[low, medium]`.
Names are checked for shape only here; an unknown name is skipped when the tier
is resolved. `tiers` with `model: inherit` is a definition error. Repeats collapse
like other list keys.

Settings edits this as a **run mode**: a subagent runs on the session model, a
specific model with ordered fallbacks, or a tier list; choosing a mode removes the
other mode's `model`/`fallback`/`tiers` keys. A file that has both opens as a specific
model with a warning.

Which tier a child runs on (first match wins; `TierDecision` in `runner.py`):

1. the call names a tier (`model: "medium"`);
2. the call names a concrete model: its tier is used, and the exact model runs
   while that tier is allowed;
3. the role's pinned `model`;
4. the role's first tier.

A tier outside the list moves to the **nearest allowed tier** (ties go to the
cheaper one) instead of failing, because a hint should never break delegation;
the report says so (`quick runs on low only; requested 'high' ran on 'low'`). The
global `max_tier` is applied last. A tier with no runnable model falls through to
the role's other tiers, then to the parent's model. A tier is only run as a tier
when the router has a registry (a plain config has none, so the child inherits
the parent's model). The permission key uses the final tier, so
`deny = ["Task(*:high)"]` keeps working. `agent.spawned` carries `allowed_tiers`
and the requested tier. A pinned model whose tier is not listed is not blocked:
calls move to the nearest listed tier, and Settings points the conflict out.

The roster in the `subagent` tool description shows each role's tiers
(`task [tiers: low (default), medium]`) and a short guide on when to use each
tier ([tools.md](tools.md)). Settings → Agents edits `tiers` for every subagent
([surfaces.md](surfaces.md#settings)); `nexus doctor` hints at subagents without
any.

Refusals (unknown role, depth, budget) return as error outcomes, not exceptions.
The runner appends every file a child changed (≤ 100) to its report.

Children are real sessions in namespace `agents` (`<parent>/sub/<n>`). Their
events relay to the parent log through an isolated per-child relay stamping an
`agent` block (id, parent, depth, type, task, tier), so a UI rebuilds the tree
and replay folds it from the parent log alone. Lifecycle, context, permission, input and presence events stay in the child's
own log and are not relayed (`_CHILD_RELAY_SUPPRESS` in `runtime.py`): a relayed
child `turn.completed` would look like the parent ending, and a relayed
`permission.requested` would become a parent approval. The child's first
`context.assembled` records the request it actually sent; the subagent page
reads it back through `AgentTranscript`. Events: `agent.spawned`,
`agent.completed`, `agent.clamped`.

## Failed subagents keep their context

A child that stops without finishing (`max_iterations` or the wall-clock/token
budget, a provider error, a crash) returns `status: failed` with a **handoff**
(`agents/handoff.py`): task given, stop reason, last assistant message, files
changed, and the last 30 tool calls with errors flagged. It is built
deterministically from the child's durable session, with no model call, so it
works even when the child failed because of the clock. The Task result carries a
bounded digest; the full report is saved to `<nexus home>/handoffs/<session>.md`
(best effort; a failed write still returns the digest and says it was not saved).
There is no resume field on the `task` tool: hand-offs are rare, so the root
decides itself whether the work still matters and starts a new `task` whose
prompt carries what it needs from the digest (or the saved file); the failed
child's edits are already in the workspace. The report is not a
model-written summary; not verified: whether a final model wrap-up turn near the
limit would be worth its cost.

## Worktrees

`subagent(worktree=true)` runs the child in an isolated Git worktree on a branch
`nexus/subagent/<…>` so parallel children cannot edit the same files. Children in
a worktree get only `WORKTREE_CHILD_TOOLS` (read, glob, grep, edit, write,
apply_patch, subagent, todowrite, question, skill, webfetch, websearch), no shell.

Lifecycle: `active → finalized → integrated | cleanup_pending | discarded`.
Records are authenticated and held under a service-root lock.

| Step | Command (`host/protocol.py`) | Behavior |
| --- | --- | --- |
| list / inspect | `WorktreeList`, `WorktreeInspect` | sanitized metadata only, no paths |
| review | `WorktreeReview` | immutable snapshot built with Git plumbing and filesystem reads, comparing the recorded base tree to the final filesystem (commits and index-only changes included); ≤ 500 files, 16 MiB per file, 128 MiB total, 128 KiB pages |
| acknowledge | `WorktreeAcknowledge` | the user confirms an exact `review_id` + digest |
| integrate | `WorktreeIntegrate` | applies the frozen bytes to a **clean** parent checkout without Git commit/reset/stash/checkout; a private journal is published before the first mutation so an interrupted transaction rolls back (`recover_transactions`) |
| discard | `WorktreeDiscard` | preview then confirm; removes only a worktree Nexus owns |

CLI: `nexus worktrees list|inspect|review|acknowledge|integrate|discard`; chat:
`/worktrees`. Both UIs share one confirmation flow.

## Changing agents

- Adding a packaged role: add `data/<name>.md`, list it in `SEEDED_ROLES`, and
  bump `SEED_VERSION` only if seeding semantics change.
- New frontmatter key: `model.py` (grammar, limits, validation) and Settings'
  `ui_support/agent_frontmatter.py`/`host_support/settings_inventory.py`.
- Tests: `tests/test_agents_manager.py`, `test_agent_selection.py`,
  `test_agent_route_defaults.py`, `test_subagent_runner.py`, `test_subagent_worktrees.py`, `test_worktree_*.py`,
  `test_host_worktrees.py`.

## Deferred MCP authority

Agent intersections include deferred target names without advertising their
schemas. `tools: [mcp__github__*]` and individual qualified names select only
matching targets. The search/call proxies are included when a target survives
the intersection; search results and preparation enforce that same ceiling.
Children inherit the root session's frozen modes. Read-only roles strip mutating
targets, and proxy execution cannot turn a deferred name into new authority.
