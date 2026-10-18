# Agents, subagents and worktrees

`nexus/agents/` (L3) discovers agent definitions, runs bounded nested subagents
through the `subagent` tool, and manages isolated Git worktrees for children.
The concrete child runtime is built by `Runtime` (`_ChildRuntime`); the runner
never imports it.

## External Claude Code workers

An explicit `claude-code/<model>` role (a `provider`/`model` naming a
`[providers.<name>]` section with `kind = "claude-code"`) is **not** run by a
Nexus `run_turn` loop. `_ChildRuntime._plan_external` resolves the policy before
anything starts and `_run_external` hands the assignment to
`agents/claude_code_worker.py:ClaudeCodeWorker`: one fresh interactive Claude
Code session per assignment, in a Nexus-owned PTY, `cwd` = the assigned
workspace or worktree, using **Claude's own tools**. Nexus tool grants,
`PathGuard` and the permission engine do not mediate them (see
[security.md](security.md#external-claude-code-worker)). Every other child is
unchanged; ordinary children never see the `claude-code` provider. There is no
default model: a route that names no exact model (a bare `provider = "claude-code"`
role with a tier or alias) is refused at start. The model must be an exact
`claude-<family>-N...` ID, never an alias. Main-agent selection of
Claude Code still uses the Agent SDK provider and is hidden unless
`[models] claude_code_main_enabled = true`.

**Backend switch.** `[models] claude_code_subagent_backend` (Settings -> Models,
"Claude Code subagents run via") picks how these children run: `worker` (default,
everything in this section) or `sdk`. With `sdk`, `_ChildRuntime` plans no external
run: the child is a normal Nexus `run_turn` child whose router includes an Agent SDK
`ClaudeAgentProvider` for each `claude-code` route (the root's when main selection
built one, else one the child builds and closes itself). Claude's built-in tools
are off there; it uses the child's Nexus tools under Nexus permissions and
`PathGuard`, usage is measured, and the routing guards below (inheritance,
worktree) do not apply. Enabling `sdk` does not make Claude Code a main model; main-agent routes still require `claude_code_main_enabled = true`. Worker-backed Claude Code is omitted from main model choices but available from child model/tier pickers even when the main toggle is off. Antigravity appears only in subagent model choices, including the native model dropdown.

**Tiers.** A `[models.tiers]` entry may name a `claude-code/<model>` route
(`low = ["claude-code/claude-haiku-5-5", ...]`). For **children only**,
`Runtime._claude_code_tier_route` walks the tier's candidates in order: the first
that is a configured `claude-code` section runs on the worker, but a Nexus
provider that is runnable and listed first wins. The runner's runnable probe
counts such an entry, so a tier of only `claude-code` routes does not silently
fall back to the parent's model. The root's router never receives `claude-code`
(it cannot stream it), so a root agent that resolves the same tier skips it.
Verified live 2026-10-13: a `low` subagent ran as `external.started`
(`claude-haiku-5-5`, `auto`, restricted) and its own `Read`; the daemon of the
workspace must be restarted after config changes (`nexus daemon restart`).

One worker can be stopped alone with host `AgentCancel` ([host.md](host.md#cancelling-one-child-agent));
the native client shows the worker's authority, tool rows, status and unknown usage
([ratatui-parity.md](ratatui-parity.md#external-claude-code-workers)).

**Routing guards.** The route must be explicit: named by the `Task` call, by the role
(`model`/`provider`) or by a tier. A child that would only *inherit* the parent's
`claude-code/...` model is refused at start (`ConfigError`, reported as a failed start),
because Claude's own tools are not mediated by Nexus. A worktree child (`worktree: true`,
or a descendant of one) is refused too: the worktree profile is the reviewed no-shell
tool set, and Claude's tools (and any shell `worker_tools` grants) are not bound by it.
Use a non-worktree child for Claude Code.

**Role instructions and context.** Claude Code has no Nexus system-prompt channel, so
the role body is sent as a labelled prefix of the single prompt
(`Role instructions (Nexus agent role '<name>'):` ... `Task:` ...). The 64 KiB prompt
limit applies to the combined text (refused, never truncated). The child's log records
a `context.assembled` request snapshot (redacted, bounded) that states exactly this:
the instructions delivered (or that none were), the worker's authority sentence, and
Claude's tool names from the policy; `Runtime.agent_request` returns it with the task
prompt as the user message.

**Failure and cancellation.** Raw failure text (worker exceptions, start refusals, the
child's `turn.failed`, handoffs) is secret-redacted. A cancelled external child keeps its
disclosure: the runner's cancelled outcome carries the authority line, `usage: unknown`
and the file changes Nexus verified after the stop (the child leaves it in
`ChildSpec.metadata["external_partial"]` for the runner). Per-child escalation state is dropped when the child ends,
whatever the outcome.

Requires POSIX and Claude Code >= 2.1.296; the preflight probes `--version` and
`--help` and refuses to start if a required flag is missing.

**Policy** (`[providers.<name>]` `worker_*` keys, [config.md](config.md#claude-code-worker-keys)):
`resolve_policy` produces the argv: `--permission-mode`, `--setting-sources`
(default empty, plus `--disable-slash-commands`), `--strict-mcp-config` with
`--mcp-config` (explicit JSON or an empty server list), `--no-chrome`,
`--restricted` (default), `--tools`, `--allowedTools`, `--disallowedTools`, and
`--session-id`. `AskUserQuestion` is always added to `--disallowedTools` (and so
to the disclosed authority): Nexus never answers a question menu, so one would only
stall the worker indefinitely. `bypassPermissions` and `--dangerously-skip-permissions`
are never passed (and rejected at config load). `auto` is accepted as an explicit
opt-in: Claude's own classifier then answers approvals (verified live on Haiku
5.5: writes proceeded without a prompt; a risky action is denied with a "denied by
the Claude Code auto mode classifier" tool error and the model stops to explain),
so Nexus no longer decides those approvals. With the default policy
(`dontAsk`, no allow rules, restricted) anything not pre-approved is denied, so
edits or shell need explicit `worker_allowed_tools` (and `worker_tools` naming
`Bash`).

**Read-only is prompt guidance only.** No role or child is run as a restricted
read-only Claude worker: the runtime always resolves the policy with
`read_only=False`, so `advisor` and the legacy `explore`/`plan`/`planner` get the
same worker policy as any other child (user decision 2026-10-17, see
[decisions.md](decisions.md)). The `resolve_policy(read_only=True)` branch
(`--tools Read,Grep,Glob`, `dontAsk`, refusing wider `worker_tools`) stays in the
worker module but no Nexus caller uses it. A `worker_mcp_config` or
`worker_setting_sources` is still refused under the default restricted policy,
since MCP servers, plugins and hooks can act beyond built-ins. Token and cost budgets are also
refused; Nexus iteration caps are not mapped. No wall clock maps either:
external workers (Claude Code and Antigravity) and native subagents have no
deadline; `[agent] max_turn_seconds` applies to root turns only. A worker ends
on its own terminal event, a block, process exit, or user/parent cancellation.

**Readback.** An isolated `python -I` hook script (SessionStart, Stop,
StopFailure) writes atomic result files and checks the session id; its
`transcript_path` is used, else a best-effort path under the Claude config dir.
The JSONL transcript is read incrementally (1 MiB per read, 8 MiB per line, 32
MiB total), other session ids and sidechains are ignored, entries are
deduplicated by `uuid`, and reading continues 2 s after Stop to catch late
flushes. A quiet PTY, or a process that exits without Stop, is never success.
Permission-menu detection removes the echoed prompt and
Claude's own replies (from the transcript) before matching, best effort.
The terminal tail is kept only as an ANSI-stripped, redacted diagnostic of at
most 1500 characters.

**Statuses** (`SubagentOutcome.status`, with `stop_reason`): `completed`
(`end_turn`; Stop), `failed` (`error`; StopFailure, or exit without Stop),
`cancelled` (`cancelled`), `needs_attention` (trust, authentication or onboarding blocked,
model rejected, or a permission prompt while the mode is not `dontAsk`). The
attention result names the exact command for the user to run (for example
`cd <workspace> && claude` for trust, `claude auth login`). The only bytes Nexus
ever writes to the PTY are cursor-position replies: it answers no approval,
trust or login prompt.

**Outcome.** Usage is `unknown`, not zero (`metrics.usage = "unknown"`,
`AgentView.usage_unknown`, render line `[usage: unknown — external Claude Code
worker]`). The render also carries an `[authority: ...]` line. `files_changed`
is computed by Nexus from a before/after git-status plus mtime snapshot of the
workspace, plus the files touched by any commits made in between (`HEAD` moved),
never from Claude's claims; gitignored files are not checked. It is workspace-wide
(concurrent writers can appear), capped at 100 paths, and carries an announced note.
The `context.assembled` snapshot lists Claude's tools from the policy, or one
"Claude Code default set" entry (with the disallowed tools) when none are pinned. The transcript
path, Claude session id, tool-call count and clip notes are in `metrics`
(`SubagentOutcome.to_dict()["external"]`). `failed` and `interrupted` outcomes
get the usual handoff. Prompt (64 KiB), tool inputs (4000 chars per string), tool
results (8000), the answer (64000) and transcripts are bounded, and clipping is
announced.

**Events** ([events-and-view.md](events-and-view.md#external-worker-events)):
`external.started`, `external.tool`, `external.finished`, plus ordinary `text`
events (provider `claude-code`). Claude's tool calls are never `tool.*` events.

Not implemented: host inspect/cancel/attention/handoff commands, native Ratatui
rendering of `external.*` rows, and a web port. After a daemon crash, session
recovery marks unfinished external runs and their child-task rows interrupted;
it does not kill orphan Claude processes or resume them. Verified live (2026-10-13, Claude Code 2.1.296, one account, read-only policy):
the workspace-trust prompt wording, the transcript path
(`~/.claude/projects/<cwd with / and . as ->/<session>.jsonl`), the hook-driven
`Stop`, and a Claude-owned `Read` call on `claude-haiku-5-5`. Not verified live:
permission-prompt wording, `StopFailure`, and Sonnet/Opus 5.5 IDs (only Haiku was
tried; other coverage is offline fakes in `tests/claude_code_fakes.py`). Trust is
inherited from a trusted parent folder. Worktree children are refused (see
Routing guards), so that case is not exercised. Subscription
efficiency and product-use terms are not established by a PTY.

## External Antigravity workers

An explicit `antigravity/<model>` role (a `[providers.<name>]` section with
`kind = "antigravity"`, alias `agy`) runs Google's Antigravity CLI as an external
subagent. It is the headless counterpart of the Claude Code worker: the earlier
"ZMX" design ran Claude Code in an interactive PTY, whereas `agy` is driven
headless over pipes, with no PTY. `_ChildRuntime._plan_antigravity` resolves and
validates everything before anything starts, and `_run_external` hands the
assignment to `agents/antigravity_worker.py:AntigravityWorker`, which returns the
same `WorkerResult` duck contract and emits the same `external.*` events
(provider `antigravity`). Config is in [config.md](config.md#antigravity-worker-keys).

**Status: implemented and tested offline with fake `agy` processes only.** No real
model turn was run and no credentials were used. The event shapes follow the
documented headless mode (`antigravity.google/docs/cli/headless`, fetched by the
parent session) and local `agy --version`, `--help` and `models` output on agy
1.3.4. The `/cli/usage` page returned 404, so nothing here claims to be read from a
usage URL. Event shapes live in `antigravity_worker.SCHEMA` so a real run can
correct them in one place.

**Launch.** `agy --input-format stream-json --output-format stream-json --model <id>
--mode <auto|accept-edits|plan> --print-timeout <n>s [--dangerously-skip-permissions]` with `cwd` = the assigned
workspace. There is no `-p` prompt and the prompt is never on argv: it is written to
stdin as one stream-json user event (`{"event":"user","message":{"content":...}}`,
64 KiB cap, refused rather than truncated, role instructions as a labelled prefix
as for Claude). stdin stays open until the terminal `result` event and is then closed
so agy exits: in streaming mode stdin EOF ends agy's session, so closing it right
after the prompt can abort the turn. `--print-timeout` is a Go-style duration in
seconds; Nexus sets no deadline, so it passes an effectively unbounded value (one
year) because agy requires the flag. The child leads its own
process group, signalled on completion, cancel and close. The preflight
probes `--version` and `--help` (time-bounded, collecting stdout and stderr) and
refuses a missing flag. `--input-format stream-json` itself selects headless
streaming (`--print`/`-p` is not passed; its prompt is dropped in this mode). POSIX only. See [startup troubleshooting](antigravity-troubleshooting.md)
for settings defaults and daemon restarts.
The child environment is the fixed allowlist; agy uses its own cached login.

**Readback.** stdout is NDJSON keyed by `event`: `init` (cwd, tools, permission
mode, model), `step_update` (agent text deltas and tool steps) and a terminal
`result`. Success needs a prior `init` and a `result` whose `status` is exactly
`SUCCESS` with a response string. EOF, a non-zero exit, a malformed or unknown
terminal status, or an `init` reporting another cwd or model, or the
`always-proceed` permission mode (unless the bypass opt-in below is on), is never success; the partial answer is kept.
Unknown event types are only counted. stderr is a bounded, ANSI-stripped, redacted
diagnostic. Usage is whatever the terminal `result.usage` carried (`reported`);
otherwise, and always for cost, it is `unknown`, never estimated or zero.
`files_changed` is measured by Nexus exactly as for Claude.

**Statuses:** `completed`, `failed`, `cancelled`, and
`needs_attention` when agy needs sign-in, workspace trust or a permission approval.
Nexus never answers those: it writes only the prompt, so the attention result tells
the user to run `agy` interactively, or adjust agy's own permission settings, and
re-run.

**Routing and refusals.** The route must be explicit (a `Task` call, role or
tier); inheriting a parent's `antigravity/...` model is refused, as is any route
without an exact model ID that `agy models` reported (a refresh hint follows).
A tier entry such as `low = ["antigravity/<id>"]` routes ordinary child
subagents only; the root never receives it, and tiers are not ranked by price.
Read-only is prompt guidance only (user decision 2026-10-17): a read-only role
(`advisor`, `explore`, `plan`, `planner`), a read-only tool list or an empty
`tools: []` is never a reason to skip or refuse an Antigravity route, so the first
runnable tier entry wins and an `advisor` on an `antigravity/...` tier plans on agy.
Refused with `ConfigError`, never ignored, because agy's own tools are not
mediated by Nexus and no Nexus control reaches them:

- tool restriction profiles (`worker_tools`, allow/deny lists, MCP config,
  setting sources, `worker_restricted`);
- worktree children;
- token and cost budgets;
- unsupported section fields, and an explicit
  `worker_inherits_agy_settings = false`.

Inheritance defaults to `true`, even without a provider section: agy uses its own
user/global settings, MCP servers and plugins. Omission allows worker execution;
set the field to `false` to opt out.

Permission bypass is off by default. A user can set
`worker_dangerously_skip_permissions = true` in their **user-level** config
([config.md](config.md)) to pass `--dangerously-skip-permissions`; agy then
auto-approves all its tools and an `always-proceed` init is accepted instead of
refused. Project config cannot enable it. It applies only to Nexus-managed Antigravity
workers started by the Nexus runtime, not to external harness/tool integrations that
launch agy directly (use that harness's own permission settings or another backend;
see [antigravity-troubleshooting.md](antigravity-troubleshooting.md)). Every result, the start/finish events
(`skip_permissions`) and the UI header disclose it. Without the opt-in, agy's
headless soft-denials still end as `needs_attention`.

The bypass is not mediated by Nexus (no `PathGuard`, permission rules or tool
dispatch), is only a user-level or session/flag override, and does not relax the
tool/MCP, worktree or budget refusals. With it enabled and
`worker_permission_mode = "auto"` (the default), `--mode accept-edits` is passed
instead, since `auto` is not a documented agy mode; explicit `accept-edits` and `plan`
stay as configured, and preflight requires the passed mode and
`--dangerously-skip-permissions` in `agy --help`. Use this opt-in rather than an
external ZMX/Claude Code substitution.

There is no `PathGuard`, permission-engine or tool-dispatch mediation of agy's
tools ([security.md](security.md#external-antigravity-worker)). Antigravity is
subagent-only: never the root, main, plan, fast, fallback or title model.

Not implemented: automatic `agy -p /usage` (the usage panel says to run `/usage`
yourself), a sandbox request, a browser surface, and a live-verified event schema.

## Files

| File | Owns |
| --- | --- |
| `model.py` | restricted frontmatter grammar, immutable `AgentDef`, limits (`READ_ONLY_ROLES` is empty: no role is enforced read-only) |
| `manager.py` | `AgentManager`: deterministic discovery, precedence, seeding, tool selection, legacy aliases |
| `runner.py` | `SubagentRunner`, `ChildSpec`, `SubagentBudget`: bounding rules and event relay |
| `claude_code_worker.py` | `ClaudeCodeWorker`, `WorkerPolicy`, `resolve_policy`: external Claude Code sessions |
| `antigravity_worker.py` | `AntigravityWorker`, `WorkerPolicy`, `resolve_policy`: external headless `agy` runs |
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
`advisor` (advises; read-only is prompt guidance only), `task`, `quick`. They are global, not copied into
workspaces; editing one in Settings writes an override to `~/.nexus/agents/`.
Settings presents one shared Agents page, with no global/project selector; model,
effort, fallback and prompt edits are saved to the user override. File-based
workspace definitions still follow the discovery precedence below.
`agents.seed_roles` opts a workspace into seeding `.agents/agents/` (once per
marker). Legacy names: `general` → `task` (`build` as a root); `explore`,
`plan`, `planner` → `advisor`.

The packaged root prompts are short (a few hundred tokens): the Nexus identity
line, then how to work (understand, change only what was asked, verify,
delegate with self-contained briefs) and what to report. Orchestrator never
edits code itself; it plans, delegates, and verifies subagent reports. Neither
names a specific tool or role: those come from the tool list and the
`subagent` roster at runtime.

**Precedence** (low → high): packaged < `~/.nexus/agents/` < `<workspace>/.nexus/agents/`
(legacy) < `<workspace>/.agents/agents/`. Lookups are case-insensitive;
deleting an override resurfaces the lower one. Every skip, shadow, collision and
forbidden declaration is kept as a diagnostic, never silently dropped.

**Declarations are not grants.** `bundles`/`tools` only narrow. No role is
structurally read-only: `advisor` and the legacy aliases inherit the parent's
ceiling like any role (its prompt says to avoid writing unless asked). The
"read-only" wording is system-prompt guidance only, never enforced (user decision
2026-10-17).

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

**Identity colour.** An agent's presentation colour is keyed on its definition
name (`child.type`, the spawning tool's `subagent_type`/`agent` argument, or
`"task"`), never on run-specific session or task IDs. The colour is
deterministic across runtimes and sessions (a fixed SHA-256 hash in
`agent_color`, with packaged semantic colours for `build` and `orchestrator`).
An explicit `color` in the agent definition frontmatter always wins.

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
3. **Tiers select subagent routes only.** The root's model/price does not cap
   its subagents. The highest-tier setting is removed; legacy `agents.max_tier`
   is accepted for config compatibility but ignored by runtime wiring. Role
   `tiers` restrictions and nested-parent tier bounds remain, with clamping
   reported. An explicit tier selects its ordered model list even for a role
   without `tiers`; only an omitted/inherited model inherits the parent's model.
   The tool description lists every tier's provider/model IDs in order and
   instructs the caller to call the next model if a provider/model fails (passing
   along the failure and partial work). This instruction does not bypass tool
   permission checks or cause unsafe partial-stream replay.
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
nested-parent tier bound is applied last (there is no global ceiling). A tier with no runnable model falls through to
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

After a daemon crash, reopening the parent under its exclusive lease closes
unfinished spawned children as `interrupted` in the parent log, including native
children not yet promoted to durable sessions and nested children (nested-first,
preserving their parent-agent routing). Completed children are not closed twice,
and a live turn's lease prevents recovery by another manager. The root
re-delegates interrupted work; recovery never reruns tools or resumes child
execution. The same recovery also repairs owned child logs: proven child rows are
traversed recursively, each under its own non-blocking exclusive lease (a live
child and its subtree are skipped; unknown legacy logs are not guessed), and
dangling tool uses get error results ([sessions.md](sessions.md)).

**Tier is not model.** A tier (`low|medium|high`) is a routing label; the model a
child actually ran on is a separate fact, always shown beside it:

- `AgentView.model` is only the spawn-time reference (often the tier);
  `AgentView.resolved_model` is the concrete `provider/model` of the child's
  latest relayed `model.started` or `external.started` (nested children tracked
  per agent, replayed seqs ignored, an already-qualified id never doubled). It is
  omitted from the wire until a model starts, and `agent.completed` backfills it
  (`resolved_model`) only when no start was relayed.
- `SubagentOutcome.models` lists the child's model transitions in
  chronological order, collapsing only consecutive repeats (`A A B A` becomes
  `A, B, A`), from its durable child events (`_child_models` in `runtime.py`)
  or, for cancelled/crashed/fake children, the runner's relay. The last is
  `actual_model` (so a child that recovered to its original model reports it);
  more than one entry means a provider fallback.
- The model-facing report starts with `[model: <provider/model>; tier: <tier>]`
  (plus `; fallback path: a -> b`). A child with no reported model (refused,
  failed to start, crashed before the first request, or an external worker whose
  `external.started` carried no model identity) says
  `[model: unavailable (not reported); tier: …]`: it is never guessed from the
  tier, and the wording does not claim whether a request started.
  `to_dict()` carries `actual_model` (or `null`) and `actual_models`; the `Task`
  tool result metrics repeat both.

## Attention goes to the root, then the human

Child event persistence distinguishes replay state from optional observation.
`todo.updated` must be appended to the child's log and relayed to the owner's
durable sink before the tool succeeds; failures propagate through nested
relays. Other telemetry remains best-effort. A failed child-log append still
attempts the owner relay and records an `error` with code
`child_log_persist_failed` there (without raw exception text); it never claims
that the child copy was durable. Optional UI fan-out remains best-effort.

A child never asks the human directly. When it is blocked on an approval it
cannot get, the block travels up as **attention** on its `SubagentOutcome`
(`attention` in `to_dict()`, and a `[needs attention: ...]` line in `render()`):

- a Nexus child whose tool call was denied because no approver was attached
  (`permission.resolved` with an `unattended*` code; collected by
  `_ChildEventSink`, `runtime.py`);
- a Claude Code worker stopped at a trust, authentication or permission prompt
  (`needs_attention` status), or whose `auto` classifier denied an action (the
  child may still finish; its status stays `completed` and the denial is listed).

A Nexus child can also report a block itself with the **`escalate`** tool
(`tools/builtin/escalate.py`; `needed` and `why`, at most 5 per child). It exists
only in a child's catalog (added after the parent ∩ role ∩ requested
intersection), never the root's; it grants nothing, never waits, and is allowed in
every permission mode (`_ALWAYS_ALLOWED_TOOLS`; an explicit `deny` rule still
wins). The call is a normal relayed tool row, and the runtime folds it into the
same `attention`. Claude Code workers have no such tool; their blocks are detected.

The rendered report tells the root agent it is the escalation point: resolve it
first (re-delegate a narrower task, another role or workspace), and otherwise
ask the user in its reply, saying exactly what is needed and why. The root can
never grant itself or a child more authority than it holds, so a human answer is
the only way authority grows. There is no pause/resume: a blocked child ends and
the root re-delegates after the answer. Not implemented: a live handoff into the Claude PTY (the root escalates by ending its
reply). Offline tests: `tests/test_agent_attention.py`,
`tests/test_claude_code_worker.py`.

## Failed subagents keep their context

A child that stops without finishing (`max_iterations`, a provider error, a
crash) returns `status: failed` with a **handoff**
(`agents/handoff.py`): task given, stop reason, last assistant message, files
changed, and the last 30 tool calls with errors flagged. It is built
deterministically from the child's durable session, with no model call, so it
works even when the child's own model is unavailable. The Task result carries a
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

## The manager is not a role

The workspace manager ([manager.md](manager.md)) is deliberately **not** a role
file. Its prompt (`agents/manager_prompt.py`, `MANAGER_PROMPT`), pinned model
(`github-copilot/gpt-6-luna`) and six-tool catalog are packaged constants, so
no user, project or extension file can extend or replace it. The grammar
rejects `contexts: [manager]`, `bundles: [manager]` and `profile: manager`
(reserved), and a session of kind `manager` ignores root-agent selection
(`select_session_agent` and `select_session_model` refuse it). Agent
`fallback`, `tiers` and `model` are never applied to it.

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
Children inherit the root session's frozen modes. Proxy execution cannot turn a deferred name into new authority.

Antigravity defaults to `--mode auto` (the installed CLI advertises this mode);
explicit `worker_permission_mode` still overrides it. Auto is not a sandbox or
read-only enforcement, and read-only is never enforced on any provider. A
pre-start refusal durably records `selected_model` separately from
`resolved_model`, and shows the selected provider/model as **not started** plus
the redacted configuration/policy error, never a claim that the model ran.

Same-session handoff does not spawn or reparent an agent. Explicit child owner,
root and logical identities remain unchanged; the child log's handoff event is
not relayed into its parent's context timeline. External workers retain their
own context lifecycle. `/agent reset` changes selection, `/fork` creates a new
identity, and `/handoff` retires model context inside the existing identity.
