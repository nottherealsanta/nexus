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
tried before `models.fallback`), `color` (`#RRGGBB`, else derived from the name),
`max_iterations`, `context_tokens`, `contexts` (`root`, `subagent`; default
subagent-only), `profile`, `write_roots` (`global`, `project`: narrows mutating
file tools to `~/.nexus/` and `<workspace>/.agents/`, never widens; credentials,
sessions, cache, trash and daemon files stay blocked).

**Packaged roles:** roots `build` (default) and `orchestrator`; subagents
`advisor` (read-only), `task`, `quick`. They are global, not copied into
workspaces; editing one in Settings writes an override to `~/.nexus/agents/`.
`agents.seed_roles` opts a workspace into seeding `.agents/agents/` (once per
marker). Legacy names: `general` → `task` (`build` as a root); `explore`,
`plan`, `planner` → read-only `advisor`.

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

## Spawning (`subagent` tool)

`subagent(prompt, subagent_type?, tools?, model?, description?, worktree?)` runs a
named role or an ad-hoc agent. The child does not see the parent conversation:
the prompt is the whole assignment.

Four bounds are computed **before** the child exists, so a child can never gain
authority its parent lacks:

1. **Tools intersect:** `parent ∩ role ∩ requested`; dropped names are reported back.
2. **Permissions inherit:** the parent's snapshot and session grants pass through
   unchanged; `deny` stays absolute.
3. **Tier is capped** at `agents.max_tier` (`agent.clamped` is emitted). A bare
   tier hint does not replace a role's configured model; with no role model the
   child inherits the parent's. Saving an agent definition applies to the next
   child call, even mid-turn.
4. **Budgets:** a shared `SubagentBudget` bounds depth (`max_depth` 3), concurrency
   (`max_concurrent` 4), fan-out (`max_fanout` 16) and aggregate tokens/cost
   (`token_budget`, `cost_budget`) across the whole tree.

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
