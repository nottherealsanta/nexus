# Subagent continuation plan: send another message to a completed subagent

Status: **v1 implemented in the working tree (P0-P3 and the code half of P4).** See
[section 15](#15-implementation-outcome) for what shipped, the deviations, and what is still open; the sections
below are the original plan and are kept as written. Sections marked *Current* describe the code as read when
this plan was last revised; *Proposed* sections were the target. Line numbers drift. Where code and this plan
disagree, the code wins ([docs/README.md](../docs/README.md)). Plans live flat in `plans/` while active and move
to `plans/done/` when finished; this file stays here until section 15's open items are closed.

## 1. Intent

Let the root agent continue a subagent that already reported (typical case: `advisor`, consulted twice about the
same design) so the child keeps its conversation, tool results and reasoning instead of the root re-pasting
everything into a fresh child.

Two things must stay separate throughout this plan:

- **Nexus transcript reuse** (the contract): the child's durable SQLite session is loaded and a new user message
  is appended. This is deterministic and under our control.
- **Provider prompt caching** (an optional provider optimization, no guarantee): a later request that starts with a
  byte-identical prefix *may* be billed/computed more cheaply. It reduces input processing cost and latency for
  the cached prefix only; the new input and all generated output are still computed and billed. It is never part
  of the contract (section 7).

### Non-goals

- Reviving background/detached subagents. The foreground-only decision stands
  ([decisions.md:1334](../docs/decisions.md)): continuation is a **blocking** call, no report wakes, no queue, no
  steering of a running child.
- Subagents spawning subagents (root-only spawn is unchanged, nesting guard near `runner.py:1750`).
- Resuming a child that was running when the daemon crashed (recovery marks it `interrupted`).
- Provider-side session objects as storage of record. Nexus's SQLite log is.

## 2. Current evidence

| Fact | Where |
| --- | --- |
| `subagent` always blocks; schema has no resume; to retry, root starts a new child | `docs/agents.md:399-410`, `nexus/tools/builtin/task.py` |
| `spawn` = `_prepare` (validate, route, reserve budget, worktree, `agent.spawned`) + awaited `_execute` | `nexus/agents/runner.py` (~1701-1722) |
| `TaskRequest.subagent_type` defaults to `DEFAULT_CHILD_TYPE` (`"task"`); `from_value` fills it with `value.get("subagent_type", DEFAULT_CHILD_TYPE)` | `runner.py:219-229, 291` |
| `permission_key` builds `"<subagent_type>:<tier>"` from a normalized `TaskRequest`, so an omitted type is already `task` at gate time | `runner.py:1562-1566`, `task.py:179-227` |
| `SubagentBudget.reserve()` unconditionally does `_children += 1` after the fan-out/token/cost checks; `settle` and `release_reservation` do not decrement it | `runner.py:782-838` |
| `agent.spawned` payload carries `agent` meta, description, model, requested/allowed tiers, clamp flags, selected tools, dropped tools, and `worktree` (when any). It does **not** carry workspace (when no worktree), reasoning effort, `max_iterations`, `context_tokens`, or the system prompt | `runner.py:1922-1938`; `ChildSpec` fields at `runner.py:~880-885` |
| `_run_child` calls the factory, `child.run()`, then `child.aclose()`; for native children `aclose` calls `SessionManager.evict(...)` through the facade, which drops the **in-memory handle** (it raises `SessionBusy` if in use). It does not delete or close the transcript: events stay in SQLite | `runner.py` (~2222-2263), `runtime.py:2203-2205`, `session/manager.py:358-376` |
| Native child opens the durable child session and runs one `run_turn(user_input=spec.prompt, persist_user_message=True)` under `session.begin_turn` | `runtime.py` (~2573-2688) |
| Children are durable `agents`-namespace sessions, logical id `<parent>/sub/<n>`; `resolve_child_id` finds them by id/logical id; `reserve_child_id` allocates the ordinal | `nexus/session/db.py` (~1450-1490) |
| Child system prompt is rebuilt per spawn from the role body (`system_files={"soul": spec.system_prompt}`) | `runtime.py` (~6851-6916) |
| **Reducer already supports follow-ups.** `AgentView` has `runs`, `continuable`, `stopped`, `last_run_ts`, `status`. `_on_agent_spawned` sets `runs=1`; `_on_agent_completed` takes `max(runs, data.run)` and reads optional `continuable`; `_on_agent_resumed` expects `data.message` (clipped to 4000 chars), appends a `root_followup` turn, resets status to `spawned`, increments `runs` | `nexus/view/model.py:558-612`, `nexus/view/reduce.py:1564-1726, 2076` |
| **Session recovery already handles resumed runs.** `agent.resumed` re-opens the spawned entry (`completed_agents.discard`) and tracks `runs`, so an unfinished follow-up is closed `interrupted` like an unfinished spawn | `nexus/session/session.py:2134-2166` |
| Host already has `AgentCancel`, `AgentClose` ("cannot be continued"), `AgentTranscript` | `nexus/host/protocol.py` (~516-545) |
| Legacy: completed legacy children read as *finished*; `agent.resumed` replays as history | `docs/agents.md:429-437` |
| Prompt-cache boundaries are already computed for children by `ContextManager.for_iteration` | `nexus/context/manager.py` (`for_iteration`), `nexus/context/cache.py` |
| `ChildSpec.context_tokens` exists but is currently **ignored** by the child runtime (separate existing defect) | `runner.py:~885`; verify in `runtime.py` |

Key observation: the native path is almost resumable, and the **read side is mostly built** (reducer, recovery).
What is missing is the *write side* (nothing emits `agent.resumed` today; the runner never reopens a child) and
three pieces of authority: a durable record of how the child was configured, an identity/authorization rule for
resume, and budget accounting that is not "one reservation = one new child".

## 3. Design recommendation

**Add an optional `resume` argument to the existing blocking `subagent` tool** rather than a new tool.

```
subagent(prompt, resume?: "<agent_id>", description?, ...)
```

- `agent_id` is a stable id the report **explicitly adds** (see 3.3). The normal report today does not include
  agent id or session id.
- `prompt` is the new user message only; the child keeps its original assignment and history.
- Still blocking; `concurrency="parallel"` across *different* children; **exclusive per child**.

Why not a new `subagent_send` tool: that name was removed on purpose; the registry, code-mode catalog, permission
rule family and role-file warnings treat it as unknown (`decisions.md:1339-1342`). Reusing `subagent` keeps one
model-facing tool and one permission family, `Task(<role>:<tier>)`.

Why not live idle runtimes: the removed flow's problem was idle slots and eviction. Continuation here is **cold**:
a continuable child holds no concurrency slot, process or lease between calls; only the durable log and the
immutable config record (4.1) exist.

### 3.1 Identity and `subagent_type` semantics

`TaskRequest` defaults `subagent_type` to `task`, and the permission gate normalizes the request (so the default
role is applied) before `permission_key`. A resume that simply omits `subagent_type` would therefore be
authorized as `Task(task:<tier>)`, which could **bypass** a role-specific rule (e.g. a user who allows `advisor`
but asks for `build`, or denies `build`).

Rules:

1. `TaskRequest` must preserve *omitted vs explicit*: change the field to `str | None` (or add a sentinel/flag
   `subagent_type_explicit`), and keep the default only for non-resume requests. `from_value` must not fill the
   default when `resume` is set.
2. With `resume`, `subagent_type` should normally be **absent**. If present it is validated for equality with the
   recorded role after lookup; a mismatch is refused ("a continued child keeps its role").
   `tools`, `model`, `worktree`, `context_read` likewise: absent or equal to recorded values.
3. **Resolve identity before the permission check.** `permission_key(request)` for a resume request looks up the
   stored child (ownership-checked, see 3.2) and builds the key from the **recorded role and recorded tier
   (post-clamp)**, never from the default. The tool's static key path (`_static_permission_key`) must apply the
   same rule: if `resume` is present and `subagent_type` is absent it cannot return `task:*`; it either resolves
   through the service or fails closed to the most restrictive key.
4. If the recorded role no longer exists or the tier no longer resolves, refuse before running (no silent role
   substitution).
5. Role-specific authorization must hold on resume: a user who has since denied `Task(advisor:*)` blocks resume of
   an advisor child, same as spawn.

### 3.2 Continuability

A child is continuable iff all hold at call time (checked in `_prepare`, under the per-child lease, 6.4):

1. Child row exists in the `agents` namespace and belongs to **this** root session, verified through the
   allocation/parent edge, not by string prefix. "Unknown" and "not yours" return the same error.
2. Last parent-log lifecycle for that agent is `agent.completed` with `status=completed` (v1). `cancelled`,
   `failed` and `interrupted` are later phases.
3. Not closed (`agent.stopped`) and not a legacy background child.
4. Native Nexus child (v1). External Claude Code / Antigravity are refused with an explanatory error.
5. The durable child config record exists (4.1). Legacy children without it are not continuable.
6. No worktree (v1; see 6.3).
7. Role and tier still resolve and are still eligible (3.1).

Anything else returns an ordinary error outcome telling the model to start a new subagent.

### 3.3 Report footer

`SubagentOutcome` currently reports without `agent_id`. Add to the footer exactly: `agent_id: <logical id>` and
`continuable: yes|no (<short reason>)`, plus `run: n`. Only the logical id (`<parent>/sub/<n>`) is shown; do not
print session row ids, database paths, lease ids or worktree paths unless already part of the report. This stays
inside what the root already can see through `AgentTranscript`.

## 4. Data model and contracts

### 4.1 Durable immutable child configuration (required)

`agent.spawned` is **not** a complete `ChildSpec`, so continuation must not claim to reconstruct from the event.
Missing: workspace without a worktree, reasoning effort, `max_iterations`, `context_tokens`, system prompt/role
body, role fingerprint, and the tier/route fallback list. Add an **authoritative record written once at spawn**.

Storage options:

| Option | Pros | Cons |
| --- | --- | --- |
| A. New event `agent.config` in the *child* log, written before the first turn | Replay-safe, no schema change, travels with the transcript, trashed with it | Larger first event; must be excluded from model context and from relay to the parent log |
| B. Extra fields in `agent.spawned` (parent log) | One event | Bloats parent log, relays to every UI; prompt text is sensitive |
| C. New SQLite table keyed by child id | Queryable | Migration, separate retention; DB is append-only so it needs an additive migration |

**Recommendation: A** (child-log event `agent.config`, `immutable`, never updated, one per child), with a hashed
fingerprint also added to the parent-log `agent.spawned` (`config_sha`) so the parent view can detect drift
without carrying the body. C is the fallback if reading the first child event proves slow.

Record fields (v1): schema version, role name, role fingerprint (hash of body), **effective system prompt text
(snapshot)**, selected tools, dropped tools, resolved model and ordered route/fallback list, tier and requested
tier, reasoning effort, `max_iterations`, `context_tokens`, workspace (absolute path, always), `context_read`,
worktree reference (if any), budget settings in effect.

**Role prompt policy: snapshot at spawn, resolve nothing at resume.** A continued child is the same agent, so it
keeps the system prompt it started with; this keeps behavior and the provider cache prefix stable. A changed role
file does not change a continued child. The previous draft's "use the current body" alternative is rejected:
it makes the same child change identity between runs and breaks prefix stability. Safety is preserved because
*authority* (grants, permission rules, tool availability) is always re-intersected with the **current** state
(5.1) even though the prompt text is frozen. Compatibility: for a child whose role was deleted, the snapshot still
runs only if the role's permission key remains allowed (3.1.4 says refuse); a `resume` option to adopt the new
body is not offered in v1.

Secrets-safe storage and permissions:

- Never snapshot environment values, API keys, auth headers or resolved credentials. Store model *references*,
  not provider endpoints with embedded tokens.
- The effective prompt may contain project/user text; it lives in the same SQLite file and permissions as the
  transcript (user-only file mode, no new world-readable artifact), is excluded from model-visible context, is
  not relayed to the parent log or UIs, and is omitted from exports and logs (only `config_sha` and role name
  appear there).
- If prompt assembly already inlines secrets from config/skills, redact with the existing redaction used for
  spawn task text before persisting; add a test.
- The record never stores **grants**. Authority is recomputed.

### 4.2 Events

Keep `agent.spawned`/`agent.completed`. The reducer and recovery already fold `agent.resumed`, so this is **not**
a whole-reducer change. Emit, from `_prepare` through the spawning call's emitter:

```
agent.resumed {
  "agent": {id, parent, parent_session, parent_call_id, root_turn_id, type, session, depth, ...same meta as spawn},
  "run": n,                       # previous runs + 1; int > 0 (recovery and reducer both read data.run)
  "message": "<clipped, redacted new prompt>",   # what _on_agent_resumed reads
  "model": "...", "tier": "...", "call_id": "..."
}
```

- The payload uses `message`, **not** `prompt_preview` (an earlier draft's name): the reducer renders
  `data.message` and ignores other keys. Sanitize with the same redaction as spawn task text and truncate at the
  producer to at most 4000 characters (the reducer's `_FOLLOWUP_ROW_CHARS`), marking truncation with an ellipsis;
  the child still receives the full prompt.
- A fresh `agent.completed` with `run: n` and `continuable` closes each run. Without `run` the reducer keeps
  `runs` as is, which is correct for legacy.
- A missing resumed event while a run happens is a bug: the recovery path relies on it to find an open run.

Precise remaining concerns (rather than new reducer work):

1. Nothing emits `agent.resumed` or `run`/`continuable` on `agent.completed` yet: the runner write side.
2. `agent.completed` should carry `usage` for the **run**, not cumulative; the reducer *replaces* `usage`. Decide
   whether the view shows per-run or cumulative usage and, if cumulative, sum in the runner. Test current
   semantics before changing.
3. `AgentClose` writes `agent.stopped`; confirm the reducer clears `continuable` (current code has a `stopped`
   field; assert it).
4. Consider an optional `state: running|idle|finished|closed` only if UI needs more than `status`/`continuable`/
   `stopped`; do not add it speculatively.

### 4.3 Tool/host contracts

- `task.py`: add `resume` (string). Description: "To ask a finished subagent a follow-up, pass its `agent_id` and
  only `prompt`; it remembers its earlier work. Prefer a new subagent for an unrelated task." Update `build` and
  `orchestrator` role bodies (`nexus/agents/data/`) and `docs/tools.md`.
- Host: no new request for v1. Optional read-only snapshot fields (`continuable`, `runs`) reuse what `AgentView`
  has. User-initiated continuation from the TUI/web is out of scope (tool-only in v1) because it bypasses the
  root and needs its own authority story.

## 5. Authority

### 5.1 Re-intersect, never reuse

On resume the effective tool set is `current parent tools ∩ recorded child tools`, and permissions are checked
against the **current** rules and session grants. A child spawned before the user tightened permissions or added a
`deny` must not keep old authority (`docs/agents.md:381-382`: `deny` stays absolute). Tools recorded but now
unavailable are dropped and reported (`dropped_tools`). Tools newly allowed are *not* added: the child keeps its
original (possibly narrower) set.

### 5.2 Model routing

Use the recorded model first. If it no longer routes, walk the recorded ordered fallback list, emit `agent.routed`
with `resumed=true`, and tell the root. A model change is allowed (history is provider-neutral) but forfeits any
prefix cache hit. If nothing routes, refuse.

## 6. Lifecycle

### 6.1 Resume path in the runner

`spawn` stays `_prepare` + `_execute`. `TaskRequest` gains `resume` (and the omitted/explicit type, 3.1).
In `_prepare`:

1. Parse; refuse impossible combinations.
2. `resolve_child_id`, verify ownership and continuability (3.2); load the immutable config (4.1).
3. Skip `reserve_child_id`: reuse `session_id`/`agent_id`.
4. Re-intersect authority (5.1), re-route (5.2), reserve budget with the continuation accounting (6.5).
5. Emit `agent.resumed` (4.2).

`ChildSpec` for the run is built from the recorded config, with `resume=True`. `_execute` runs the same
slot/worktree-less/settle logic and emits `agent.completed` with `run: n`. The native runtime opens the **existing**
session (the `create=True` assumption at `runtime.py:~2581` must be checked) and calls
`run_turn(user_input=spec.prompt, persist_user_message=True)` on a session with history.

After the run, `_run_child` still calls `aclose`, which evicts only the in-memory handle through the facade; the
transcript stays in SQLite. The next resume reopens it. Nothing needs to stay live. If `evict` raises
`SessionBusy` (an open viewer), the current code suppresses it; the next open simply reuses the handle.

### 6.2 Context growth and compaction

History only grows. Verify how root compaction is wired for child sessions before P2. Required for v1: if the
child cannot fit the new prompt after compaction, return "context full, start a new subagent" instead of
silently truncating the assignment. `ChildSpec.context_tokens` is currently ignored: that is an existing, separate
issue; it is a **prerequisite only if** the verification shows children cannot compact without it. Otherwise
track it independently and do not gate continuation on it. Expect each continuation to cost more input tokens
than the last; surface per-run usage (7).

### 6.3 Worktree children

v1 refuses children that had a worktree: `mark_finished` finalizes the worktree in the `finally` block, and the
integrate-to-clean-parent flow assumes one run per child. A later phase may allow it only if the checkout is
retained, still on the recorded base, owned by the same uid, clean or owned by the child, and not already
integrated; revalidate all of that at resume and refuse otherwise. Worktree lifetime then needs an explicit TTL.

### 6.4 Concurrency, lease and race

Per-child exclusivity: an in-process lock gives a friendly "already running" error; the child's session turn
lease (`begin_turn`) is the correctness authority across processes. Two resumes of one child in a batch: one
runs, one fails fast. A resume racing `AgentClose` or session deletion: re-check state after acquiring the lease
and refuse if closed. Resume racing the *spawning* call of the same child cannot occur (spawn is awaited).

### 6.5 Budget and bounded retries

Do **not** call the same `reserve()` for continuation: it always increments `_children`, which is the fan-out
counter, so a resume would consume fan-out as if it were a new child (and nothing decrements it on settle). Define
separate dimensions in `SubagentBudget`:

- `max_fanout` counts **unique children** per root turn window (unchanged semantics for spawn).
- A new `reserve_continuation(child_id, tokens, cost)` checks token and cost budgets exactly like `reserve`, but
  does not touch `_children`; it increments a **run counter** and a per-child run counter.
- Caps: `max_runs_per_turn` (total child runs per root turn, defaulting to `max_fanout`) and
  `max_runs_per_child` (default small, e.g. 4 in v1, config-backed if kept) so a model cannot loop on one child.
  Exceeding either returns a normal refusal error.
- Reservations are released on refusal/cancel and settled with measured usage as today.
- Bounded retries: a failed or cancelled resume still counts toward the run caps; the runner never auto-retries.
  The per-turn window resets with the root turn as today. Usage attributes to the run's root turn.

### 6.6 Cancellation and recovery

The child's cancel token follows the spawning call as today; on cancel, dangling tool use is repaired and the
outcome is `cancelled`. In v1 a cancelled run leaves the child not-continuable (it reports
`continuable: no (cancelled)`); relaxing this is P5.

Crash recovery already treats an unmatched `agent.resumed` as an open run and closes it `interrupted`
(`session.py:2142-2153`). Plan tests for that existing behavior first (fixture: completed, resumed, crashed), and
only add code if a gap shows: e.g. ensure the resulting child is not continuable after `interrupted`.

A user message persisted in the child log whose run failed to start stays and shows as history on the next resume.

## 7. Prompt caching: what is and is not promised

References below are plausible documentation locations. **The pages were not successfully fetched for this plan;
their contents, endpoints and numbers are not verified.** Re-read them before quoting anything in docs.

- OpenAI prompt caching: <https://developers.openai.com/api/docs/guides/prompt-caching>
- Anthropic prompt caching: <https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- OpenAI Agents SDK sessions (prior art for "idle session starts a turn"; the "active session steers" half is
  excluded here): <https://openai.github.io/openai-agents-python/sessions/>

General expectation (to be confirmed against those pages): providers may reuse a matching *prefix* of a later
request. That reduces computation/billing for the cached input and latency; it does **not** remove the cost of
processing new input or generating output, and it is subject to provider-defined eligibility, minimum prefix
length and expiry. One cached prefix therefore never "prevents" generation cost.

Consequences:

1. Never describe continuation as "cached" to the model or user. Report measured usage; record
   `cache_read`/`cache_creation` tokens per run when the provider returns them.
2. Prefix stability is what helps, and the snapshot policy (4.1) provides it: same system prompt bytes, same tool
   schema order, same model/provider, no history rewriting, compaction only when needed.
3. The cache-boundary machinery already exists: `ContextManager.for_iteration` computes boundaries for children
   too. Do not propose wiring it; only add a test that a resumed child still receives them.
4. Idle gaps beyond provider expiry, a model switch/fallback, or compaction all miss. That is correct behavior.
   No test asserts a hit against a real provider.
5. Do not store provider conversation ids as the continuation mechanism in v1; it would tie the child to one
   provider and defeat fallback.

## 8. Provider and external-worker implications

- Native Nexus children (any provider): supported from P2.
- Claude Code worker: has `--session-id` and its transcript is in Claude's store, so a later phase could resume
  it, but PTY lifecycle, hooks and trust make that a separate design. v1: `continuable: no (external worker)`.
- Antigravity (`agy`): resume support unverified. Refuse in v1.

## 9. Security and data

- Authority never widens (5.1); grants are not persisted.
- Ownership and role-key checks happen before any state change (3.1, 3.2); identical errors for unknown vs foreign.
- Prompt-injection persistence: a child that read untrusted content keeps it across resumes. Mitigate with
  `AgentClose` and role wording that prefers new children for unrelated work.
- The config record follows 4.1 secrets rules; deleting the parent session deletes continuability.
- `agent.resumed.message` is redacted and clipped (4.2).

## 10. Phased plan

**P0 Decision and contract (docs only).** `decisions.md` entry; define `resume`, identity rule (3.1), config
record (4.1), budget dimensions (6.5). Gate: review of this plan.

**P1 Characterize current read side (tests only, no behavior change).** Fixtures for `agent.resumed` with
`message`/`run`, `runs`, `continuable`, `stopped`; recovery of completed→resumed→crashed; legacy replay unchanged.
Fix only gaps the tests expose (e.g. per-run usage, 4.2 items).

**P2 Durable config and identity.** `agent.config` event written at spawn (4.1), `config_sha` on spawn,
`TaskRequest` omitted-vs-explicit `subagent_type`, `permission_key` resolution for resume, `resume` in schema
(not yet taught to models). Legacy children get no record → not continuable. Gate: tests in 11.

**P3 Native resume in runner/runtime.** `_prepare` resume branch, `ChildSpec.resume`, authority re-intersection,
routing, `reserve_continuation` and caps, per-child lease, error outcomes, `agent.resumed`/`completed(run)`,
footer, context-full handling. Behind a default-off flag (12).

**P4 Cancellation/recovery hardening and cache observability.** Cancel/crash states, per-run cache tokens,
prefix-stability audit.

**P5 Optional, separate approvals:** worktree children, `cancelled`/`failed` children, user-initiated TUI
continuation, Claude Code resume, retention/TTL for idle children.

## 11. Tests

- Runner (fake factory): resume of a completed child reuses `session_id`, does not call `reserve_child_id`, emits
  `agent.resumed` run=2 then `agent.completed` run=2 with `message` clipped/redacted; the child spec comes from the
  recorded config (workspace without worktree, effort, `max_iterations`, `context_tokens`, prompt snapshot).
- Identity/permission: resume without `subagent_type` is authorized under the **recorded** role key; an allow rule
  for `task` does not authorize resuming `advisor`; a deny on the recorded role blocks; explicit mismatched type,
  `tools`, `model`, `worktree` refused; unknown, foreign, closed, running, external, worktree, legacy (no config)
  refused with the same text for unknown/foreign.
- Config record: not relayed to the parent log or UIs; no secrets/env persisted; redaction applied; role file
  changed after spawn does not alter the continued child's prompt; authority narrowed after spawn narrows the
  child.
- Budget: continuation does not increment fan-out; per-turn and per-child run caps enforced; reservation released
  on error/cancel; tokens settle per run; exhausted budget refuses.
- Concurrency: two resumes of one child → one runs; close/delete race refuses.
- Runtime/session: second `run_turn` sees first-turn history and persists both user messages in order; evict then
  reopen works; context-full errors rather than truncating.
- Reducer/recovery (P1 characterization): current semantics, legacy replay snapshot unchanged, `AgentClose` clears
  continuable.
- Cache: boundaries still emitted for resumed children; byte-identical system+tools across two resumes; **no**
  provider-hit assertions.
- Docs/layering tests accept the new field; `subagent_send` etc. remain unknown.

## 12. Rollout and config

Recommended: **ship P2/P3 default-off behind `agents.continuation = false`** with the config plumbing done
explicitly, or, if that is too heavy, ship **no flag** in the first iteration and gate only by the presence of the
`agent.config` record (legacy children stay not-continuable). Do not mention a flag in docs unless it exists.
If the flag is built:

- `nexus/config/schema.py`: add `continuation: bool = False` (and, if kept, `max_runs_per_child`,
  `max_runs_per_turn`) to the agents config near `max_fanout`, with validation like `max_fanout`.
- Config layers: wire through the existing user/project/env layering and live-settings handling used for other
  `agents.*` keys; add tests for defaults, validation and overrides.
- Docs: `docs/agents.md`, config reference and `settings_help` text.
- The tool description omits `resume` when off so models are not taught an unavailable feature.

Old logs and old clients: additive fields only; legacy children stay *finished*. `agents.background` and
`subagent_send/stop/list` remain ignored. Rollback: flag off leaves children finished; no data to migrate.

## 13. Docs

`docs/agents.md` (the "no idle children to continue" wording at ~407 becomes a "Continuing a finished subagent"
subsection), `docs/decisions.md`, `docs/tools.md`, `docs/events-and-view.md` (`agent.resumed` payload, `agent.config`),
`docs/sessions.md` (recovery rule), `docs/models.md` (cache observability and the section 7 caveat).

## 14. Risks and open questions

1. Stale authority and permission-key bypass (3.1, 5.1) are the main correctness risks.
2. Snapshotting the effective prompt stores sensitive text; follow 4.1 and confirm DB permissions.
3. Context growth and compaction in child sessions is unverified; inspect before P3.
4. Models may over-resume; mitigate with wording, `runs` in the footer and run caps.
5. Cache expectations: report measured tokens; promise nothing.
6. References in section 7 were not fetched; treat as unverified.
7. Whether `cancelled` children should be continuable is deferred to P5.
8. Whether `agent.completed.usage` should be per-run or cumulative needs a decision in P1.

## 15. Implementation outcome

Written in one pass **without the ability to run anything** (the implementing session had no shell): nothing
below has been executed. The new and edited tests are unrun, so treat every "done" as "written and read-reviewed".
First action for the next session: `uv run pytest tests/test_subagent_continuation.py tests/test_subagent_runner.py
tests/test_builtin_task.py tests/test_subagent_background.py tests/test_view_agent_lifecycle.py
tests/test_session_crash_recovery.py tests/test_subagent_continue.py -q`, then ruff.

**Written**

- P1 (read side): no new characterization file; the existing `tests/test_view_agent_lifecycle.py`,
  `tests/test_session_crash_recovery.py` and `tests/test_subagent_continue.py` already cover `agent.resumed`
  folding, completed→resumed→crashed recovery and legacy replay. Decision on 4.2(2): the view's `usage` stays
  the **latest run's** (the reducer replaces it); documented, not changed. Gap found and fixed: `agent.resumed`
  and `agent.stopped` now set `AgentView.continuable = False` (`nexus/view/reduce.py`).
- P2: `TaskRequest.subagent_type` is `None` when omitted with `resume` (default still applied without it) and
  `resume` is a field; `permission_key` for a resume uses the recorded role and tier, else `resume:unresolved`
  (tool static key and bound key both); the immutable record is the child-log event `agent.config` (option A),
  written by `_ChildRuntime.run` before the first user message from `ChildSpec.config_record`, read back by
  `_ChildSessionFacade.child_config` (ownership checked on the stored owner edge); `config_sha` is on
  `agent.spawned`. Redaction: a role prompt that changes under `redact_secrets` is stored redacted and marked
  `prompt_redacted`, and such a child is **not continuable** (deviation: avoids silently running a different prompt).
- P3: `SubagentRunner._prepare_resume`, `ChildSpec.resume/run/config_record`, `SubagentBudget.reserve_continuation`
  (separate from `children`), `agent.resumed`, `run`/`continuable` on `agent.completed`, report footer
  (`agent_id`, `run`, `continuable`), refusals for unknown/foreign (one message), non-completed, closed, running,
  legacy background, record-less, worktree, external, missing role, moved workspace, unroutable tier.
  The runtime reopens with `create=False`, skips the context-read grant on resume, and reports only the new run's
  text and file changes (`_child_outcome(since=...)`). Role prompts: `build.md` and `orchestrator.md` teach `resume`.
- P4 (partial): cancelled/failed runs close `continuable: no`; crash recovery relies on the existing rule.

**Deviations**

- **No config flag.** Gated only by the record's presence (the plan's allowed fallback). The caps are fixed
  defaults (8 follow-ups per root turn, 4 runs per child; `DEFAULT_MAX_CONTINUATIONS_PER_TURN`,
  `DEFAULT_MAX_RUNS_PER_CHILD`) and not in `nexus/config/schema.py`, which has unrelated uncommitted edits. The
  tool description always mentions `resume`.
- **`max_runs_per_child` is counted from the durable parent log** (runs started, including failed ones), not an
  in-memory counter, so a restart cannot reset it. The per-turn cap is the in-memory window counter.
- **Lifecycle authority is the parent log**, supplied to the runner as `parent_events` (runtime reads the durable
  store); without it nothing is continuable.
- **No `agent.routed resumed=true`.** The recorded tier is re-resolved against the current `max_tier` and role
  tiers and must be runnable; the recorded concrete model is kept only while the tier did not move; model
  fallback happens in the child runtime exactly as for a spawn (the footer's model line shows it).
- **No compaction on a resumed child; refusal happens before the append.** (Revised: "no context should be
  touched inside the subagent".) A resumed child's assembler is built with `_refuse_compaction`: history is sent
  verbatim (no tool-result eviction, summary, drop or handoff; fit is judged on raw message costs) and
  `ContextFullError` is raised if any of it would be cut. The earlier draft raised it after `run_turn` had already
  appended the follow-up, which stranded a user message and ended continuability. Now `_ChildRuntime.run` calls
  `ContextManager.preflight(history + new message)` before `begin_turn`/`run_turn`; on refusal the child log is
  untouched (append-only, nothing rolled back), the outcome is `failed` with `metrics.unchanged`, `agent.completed`
  carries `unchanged: true`, `continuable` stays true and `fold_child_lifecycle` restores the previous status. The
  run still counts toward the caps. Mid-run overflow (tool results grow during the follow-up) still fails the run
  after the message is stored and ends continuability. The request prefix is *not* promised byte-identical (system
  prompt and environment block are rebuilt each call); only the stored history is sent unchanged. Tests:
  `test_a_follow_up_only_appends_to_the_stored_transcript`,
  `test_a_refused_follow_up_leaves_the_child_continuable_and_the_log_untouched` and the `refuse_compaction`/`preflight`
  tests in `tests/test_context_budget.py`. The compaction threshold is skipped for a resumed child, so refusal
  needs a true raw overflow; `ProviderError` from the dry run is also an unchanged refusal. The view keeps the
  last completed status and continuability on an `unchanged` completion (see `docs/agents.md`).
  `ChildSpec.context_tokens` is still ignored by the child runtime (stored in the record, unchanged defect).
- `agent.config` is deliberately **not** added to `AGENT_EVENTS` (it never reaches a parent log or UI and the
  catalogue tests pin the groups).

**Tier fix (resume vs. fallback)**

- Defect: a first run whose tier had no runnable model fell back to the parent's model
  (`_tiered_child_model`) but recorded the tier (e.g. `medium`) and that model; the resume check
  `_runnable(tier)` then refused with `no model is available for its tier 'medium' right now`
  (seen in `test_runtime_reopens_the_stored_child_transcript`, where no tier is configured).
- Fix (`runner.py`, resume preparation): an unrunnable tier still refuses, except when the recorded model is a
  concrete (non-tier) model equal to the parent's *current* model, i.e. the same fallback the first run used. The
  child keeps that model; it is never moved to a different one. A tier-routed child (recorded model is a tier name)
  or a parent whose model changed is still refused.
- Tests added in `tests/test_subagent_continuation.py`: continues on the parent-model fallback; refused when the
  parent model changed or is absent; tier-routed child refused when its tier goes dark.
- Not verified: the
  runner cannot probe whether the concrete parent model is itself available or authorized; it relies on the
  parent still using it.

**Open / not done**

- Cache observability (per-run cache tokens in the view) and a prefix-stability test are not done (P4 remainder);
  `SubagentUsage` already carries cache read/write tokens in the report and event.
- `docs/models.md` has no cache note; `agents.md` says only that provider caching is not relied on.
- Exports of the child log (`SessionManager.export`) would include `agent.config`; no user surface exports the
  `agents` namespace today, but this is unaudited.
- P5 (worktree, failed/cancelled children, TUI/web continuation, Claude Code resume, idle retention) untouched.

### 15.1 First run, verified

The suite was run afterwards. Fixes: `orchestrator.md` exceeded the 2400-char prompt cap and the
`never shared automatically` / `actionable brief for task` phrases tests pin (tightened wording, no behavior change);
two older tests (`test_phase6_integration.py::test_parallel_tasks_both_run`,
`test_subagent_background_config.py::test_background_true_still_runs_the_child_in_the_foreground`) compared the whole
report body and now take its first line, because section 3.3's footer (`agent_id`, `run`, `continuable`) follows it.
Plain (non-resume) spawning needed no code change.
