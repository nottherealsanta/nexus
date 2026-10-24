# Context: assembly, budget, compaction, caching

`nexus/context/` turns structured session history into one provider-ready
`ModelRequest` per loop iteration. The product idea lives here: what the model
sees is what the user can inspect (the context header in both UIs,
`ContextInspect`, `/context`).

## Files

| File | Owns |
| --- | --- |
| `manager.py` | `ContextManager`, `AssemblyEnvironment`, per-turn freeze (`for_turn`), the `PreCompact` gate seam |
| `parts.py` | composable `ContextPart`s, `EnvironmentInfo`, skills and MCP index freezing |
| `budget.py` | `compute_input_budget`, `allocate`, `BudgetPlan`, `ContextOverflow` |
| `compact.py` | pure compaction strategies |
| `counting.py` | request-aware token counting (`RequestTokenCounter`) and semantic keys |
| `cache.py` | `TokenCountCache` (disk), `CacheBoundary`, `prompt_cache_boundaries` |

## Session selections

Before the first turn, `ContextExtensionSelect` can disable tools, skills, or
MCP servers for this session. The replayed selection filters the frozen manifest
and tool catalogue before assembly: disabled schemas and index entries are not
sent to the provider. Inspection keeps selectable disabled rows with `enabled:
false` (and `config_enabled: false` for Settings-disabled tools), while tools
outside the selected agent's allowlist stay hidden. Token totals count the
selected request, not these display-only rows.

Selections change the assembled prompt/schema content and therefore its semantic
token-count/cache key. They are locked while a turn is active and after the first
`turn.started`, preserving the session's prompt-cache assumptions.

## Parts

Rendered in a fixed order (`builtin_parts()`); the number is the budget priority
(0 = required, never dropped).

| Part | Priority | Content |
| --- | --- | --- |
| `identity` | 0 | `IDENTITY_PREAMBLE`, currently empty: the role comes from the agent prompt |
| `soul` | 0 | `SOUL.md` plus the selected agent's prompt (`append_agent_prompt`) |
| `environment` | 1 | workspace, platform, profile, Git branch and status (`capture_environment`; capped by `context.limits.environment`). In every Git session it also carries a labelled `<session_worktree>` block (see below) |
| `tools` | 0 | structured schemas on `ModelRequest.tools`; never in system text |
| `agents_md` | 1 | selected session-root `AGENTS.md` (`context.limits.agents_md`) |
| `skills_index` | 1 | sanitised `name: description` lines only; bodies load via the `skill` tool |
| `mcp_index` | 2 | bounded, untrusted-data index of servers/roots/resources (≤ `MCP_INDEX_MAX_CHARS`) |
| `memory` | 1 | `MEMORY.md` (`context.limits.memory`) |
| `attachments` | 2 | placeholder (no-op today) |
| `history` | 3 | contiguous suffix of messages; compacts itself to fit |
| `user` | 0 | the current user turn |

`SOUL.md` and `MEMORY.md` resolve under the project root. `AGENTS.md` resolves
under the selected session root (the project root for main sessions). All three
use `resolve_within` (escapes fail closed; oversized files are a named error,
`max_file_bytes` default from `[ext]`). Project configuration still selects the
filenames and limits; a session root only changes the source of `AGENTS.md`.

## Header sections and literal inspection

**Session worktree block.** Every turn of every session in a Git workspace, in the
main checkout too, the environment part appends `<session_worktree>`; a non-Git
workspace gets no block (and no worktree tools). It is guidance only: Nexus
enforces none of it, and the user's instructions win.

*Facts* (labelled, re-rendered every iteration, so they follow the root after a
move and survive compaction): `Current root`, `Main checkout directory`,
`Main checkout branch`, `Main checkout changes` (clean or dirty), `Other sessions
in the main checkout` (titles, `(active)` when running), `Last move` (action,
target, reason, from the latest `worktree.moved`) and, in a worktree,
`Worktree name`, `Worktree directory`, `Worktree branch`, `Base branch`,
`Base commit`, `Worktree state` (merged / unmerged with a commit count / no
commits yet), `Worktree changes` and `Other sessions in this worktree`. They come
from `SessionWorktrees.environment_facts`, set on the frozen assembler by the
runtime each turn and iteration.

*Guidance*: create a worktree (`worktree_create`) before changing code across
files, refactors, dependency or build changes, experiments, multi-turn work, or
when another active session edits the main checkout; stay in the main checkout for
questions, reading, review, planning, running tests, and small edits the user
asked for (nothing is saved per turn there, so create a worktree to review or undo
turn by turn); decide before the first edit; call `worktree_list` first and
continue an existing worktree when the work belongs there; one worktree per piece
of work. In a worktree it adds the merge steps (commit, squash onto the base,
`git -C <main> merge --ff-only <branch>`, rebase and retry when the base
diverged, stop and tell the user if it still fails), "merge when done, without
being asked", "or leave it and `worktree_move` to main, saying the branch is kept
unmerged", and, when another session is in the worktree, "do not squash, do not
revert or rewrite its work". Rendering is `render_session_worktree` in
`context/parts.py`.

*In-band move notice.* When a move is applied mid-turn, Runtime appends a durable
user-role notice message (`MessageMeta.notice = "worktree_move"`, plus a
`context.notice` event with `kind = "worktree_move"`) at the step boundary, e.g.
"Working directory is now ⎇ brave-otter (`<path>`, branch `nexus/wt/brave-otter`,
base `main @ 1a2b3c`)…". Like the subagent-roster notice it sits at the tail,
never rewrites the cached prefix and is not a user prompt. Compaction may drop an
old notice, but the facts above always state where the session is and the last
move. The tool set can change between steps (`worktree_commit` appears only in a
worktree) and the assembler is rebuilt for the new root, so the system prompt and
tool schemas of the remaining steps are re-sent uncached once; history before the
move stays a stable prefix.

The context header and `/context` picker expose System prompt, Environment,
AGENTS.md, MEMORY.md, Skills, Tools and MCP, in that order. A populated
Environment block uses the agent color like other included context; empty blocks
remain neutral. The native terminal client also draws a `total` row with the share of the context window
(presentation only, no bars or animation; see [ratatui-parity.md](ratatui-parity.md),
"Context header"). The shared
projection recognises legacy identity/soul parts and named `core_prompt` parts;
`instructions` wrappers with a `memory` scope appear in MEMORY.md rather than
System prompt. AGENTS.md remains separately inspectable.

System prompt shows only core instructions when all known included parts
reconstruct `system_text` exactly. Missing parts, clipping, unknown parts or
unclassified instruction text keep the full literal system prompt visible.
In that fallback, separate prompt rows remain inspectable but their estimates
are unavailable, preventing them from counting the same prompt twice. Tools
and MCP schema estimates are independent. System prompt, Environment and
MEMORY.md open literal text viewers, not metadata/JSON summaries.

## Budget

```
input_budget = min(context.max_tokens, model context window)
               − effective_max_output − safety_margin
               (and ≤ model input limit − safety_margin when the catalogue states one)
```

An unknown (≤ 0) capability ceiling is ignored. Priority-0 parts are reserved
first; if they alone exceed the budget the turn fails with `ContextOverflow`
naming every oversized part and its tokens. Lower priorities are granted in
ascending priority then assembly order under their configured caps (`context.limits.*`, unset by default, so a part is uncapped and only the remaining budget bounds it); `history`
gets the remainder. The budget is advisory: it decides how much fits, it does not
fail a turn because an estimate was a few percent off.

## Compaction

Triggered when projected usage crosses `context.compact_at_fraction` (0.85) of the available input budget. All strategies
are pure (they return a fresh tuple; never mutate messages or the log) and
observable through `context.compacted`:

| Strategy | Behavior |
| --- | --- |
| `drop_oldest` | keep the newest N whole messages |
| `evict_tool_results` | replace old eligible tool-result content with its `context_note`; ids and error flags are kept so call/result pairing survives |
| `summarize` | summarize the dropped prefix into one pinned `Text` message; needs an injected `Summarizer` |
| `hybrid` (default) | evict, then summarize if a summarizer exists, else drop oldest |

**Current wiring:** `Runtime` does not inject a summarizer, so in practice
`hybrid` = evict then drop oldest, and `strategy = "summarize"` fails with an
actionable `CompactionError`. The summary artifact path
(`Session.append_summary`, `SummaryRecord`, reuse by `input_digest`) is in place
for when one is wired.

Compaction changes only the assembled copy: **omission from the prompt does not
delete history**. The durable records still hold the original. A `PreCompact`
hook may block compaction, which fails the turn before any model call
([extensions.md](extensions.md#hooks-hooks)). A tool result larger than
`tools.max_result_tokens` (25,000) is bounded at the source, and can carry a
one-line `context_note` so eviction replaces it cleanly instead of truncating.

## Token counting

Preference order: the provider's `count_tokens` (cached on disk by a canonical
semantic hash of the request, never prompt text), then the calibrated heuristic
(`HeuristicTokenizer`). The cache file stores only hash, count and timestamp;
a corrupt or truncated entry is a miss; a cache write never fails a caller. The
provider's own `usage.prompt` feeds the UI meter ([loop.md](loop.md#context-accounting)).
That meter describes the latest request's context-window occupancy. The completed
turn footer's `turn ↑… ↓…` shows provider token usage summed over every model
iteration in the turn; it is a cumulative counter, not a second measurement of
the current prompt.

## Prompt-cache boundaries

Emitted only when the provider advertises `prompt_caching`: one after the stable
system+tools prefix (`system_tools`) and one after the last stable history
boundary (`history`), as content-free metadata in `ModelRequest.metadata`.
This is why skill, MCP and root-agent choices lock after the first turn.

### Subagent roster notice (plan 06)

The `subagent` tool description lists each tier's models and effort. A change of
those lines is one accepted `system_tools` miss; the notice below adds no second
one because it lives at the tail of history, never in the system prompt or tool
schemas.

- **When:** at each loop iteration boundary, after steering is consumed and the
  iteration environment is built, before assembly. The digests come from the same
  runner that built the advertised `subagent` description (`SubagentRunner
  .roster_digests()`, carried on the dispatcher as `subagent_roster_digest`), so
  the notice follows what the next request actually advertises, including after a
  mid-turn tier reload on the extension path. Builders and `inspect_context` never
  write.
- **Digests:** models and efforts are hashed separately over the tier lines
  the description actually advertises (a line the budget dropped is not hashed). Role descriptions are not part of them.
  Effective effort is `effective_tier_effort` (`model/tiers.py`), the router's rule:
  an explicit `tier_efforts` selection, else the shipped default for a built-in,
  unpinned tier, else none.
- **State:** the latest `context.notice` event (`kind: subagent_roster`, `models`,
  `efforts`) in the session log. The first observation writes that event only (a
  baseline, no message). A later change appends, in one `append_batch`, a user
  message with `MessageMeta.notice = "subagent_roster"` and its `context.notice`
  event. A resumed or forked session compares against the stored digests and does
  not repeat.
- **Text:** `[Nexus notice] Settings changed: subagent tier models[ and subagent
  effort]. The subagent tool description lists current tiers, models and effort;
  rely on it over earlier conversation.` No model ids or effort values.
- **Alternation:** the notice is a user-role message and may follow the prompt or a
  tool-result message. Adapters merge adjacent user turns (Anthropic, Gemini) or
  send them in order; OpenCode renders every trailing user message as the request.
  `current_user_index` skips notices, so the real prompt stays in the current tail.
- **Not a prompt:** `is_user_prompt`, titles, `message_count`, `last_user` and the
  handoff "latest user input" ignore it. Compaction and handoff treat it as an
  ordinary message (old notices are not stripped: that would rewrite the cached
  prefix). Manager and child sessions never get one.
- `MessageMeta.notice` is a new optional field; ordinary messages gain
  `"notice": null` in their stored JSON.

## Per-turn freeze and inspection

`ContextManager.for_turn(..., workspace=root)` snapshots project config, shared
`SOUL.md`/`MEMORY.md`, and `AGENTS.md` from the selected root once, so every
assembly in the turn sees identical inputs; a file edit affects only the next
turn. `for_iteration(..., workspace=root)` returns an independent assembler for
that root and leaves the project manager and its snapshots unchanged. When the
root is omitted, existing callers use the project root. `Runtime.inspect_context` builds a read-only
next-turn preview (no provider count calls, no summarizer) for `ContextInspect`;
`host_support/context_preview.py` redacts and bounds it (≤ 4,000,000 chars) and
`ui_support/context.py` groups it for display
([surfaces.md](surfaces.md#context-presentation)).

## Changing context

- New prompt content is a new `ContextPart` with an explicit priority, a cap in
  `ContextLimits` if it can be large, and a display group in `ui_support/context.py`
  (and its web port `ui/web/js/context-view.js`) so the user can see it.
- Keep parts deterministic: same inputs, same bytes (prompt caching depends on it).
- Tests: `tests/test_context_*.py`, `test_anthropic_count_cache.py`.

User file attachments are part of message history, rather than the system
`attachments` placeholder. Numbered image/document references are paired with
labelled payloads in the user message and retained during replay.

## Pricing in the context metadata

`metadata["context"]["pricing"]` is `None` when the model's price is unknown, else
`{"input", "output", "cache_read", "cache_write", "tiers": [{"context", "input",
"output", "cache_read", "cache_write"}]}` (USD per Mtok; tiers ascending, a tier
applies when the prompt exceeds its `context` tokens). It comes from
`Capabilities.pricing` (the registry's `Cost.pricing()`), so the context manager
never imports the registry. Both UIs use it to mark where the price rises on the
context meter. See [models.md](models.md).

## MCP discovery context

Search-mode MCP schemas are deferred. The fixed `McpSearch`/`McpCall` pair
remains in the built-in tool group through searches. The untrusted `mcp_index`
shows connected server health, loading mode, tool count, bounded instructions,
resource roots and, for Search, names only (600 characters per server). Omitted
names are announced with a count and a `McpSearch` hint. The total index caps
at 48,000 characters across 64 servers. Inspection lists the full server
catalogue and adds effective `tool_loading`, its source, and approximate
`schema_tokens`. Header totals charge actual schemas and disclose deferred
tokens separately.

## Same-session handoff (P5)

`context.on_overflow = "handoff"` (the default) performs a durable context-head
cutover; set `"compact"` to opt out and keep the prior compaction-only behavior
(unchanged). A legacy config with no `config_version = 2` layer anywhere (and
only environment overrides, which never promote) has no `v2` section and stays on
compaction; any v2 layer (user or workspace) promotes the whole configuration, and
with it the `handoff` default. The
default was flipped under an explicit maintainer waiver of the soak and
real-provider quality gate; it is not validated by real use (see
[decisions.md](decisions.md)). `handoff_keep_fraction` defaults to
0.25 (also capped by the room the system, tools and user-tail parts leave in the window,
so a tool-heavy request cannot keep its whole history and retire nothing). After
tool-result eviction the assembler chooses a pair-safe suffix,
keeps the entire current user/tool tail, and performs at most one handoff per
assembly. Required brief/tail overflow is an actionable `ContextOverflow`, not
a rollover loop. The PreCompact gate may block the write.

A version-1 deterministic, secret-redacted brief records bounded user intent,
recent decisions/previous brief, child ownership, shell state, queue IDs,
tool errors and tool-derived changed paths. It is capped below 24 KiB and marks
truncation. `handoff_brief = "summarized"` additionally calls the session's
selected provider/model in the foreground with a 10% output reserve, no tools,
a bounded response and timeout. Empty/error recall falls back to the mandatory
deterministic brief and marks `summary_fallback`; cancellation propagates.
The brief itself uses a conservative 10%-of-input character cap on automatic
cutover, leaving room for required input. It is recall, not a new instruction
source or a guarantee of complete semantic compression. When the brief exceeds
that cap, every line is shortened to one shared length (short lines stay whole)
and `[truncated]` is appended, so the history pointer, intent, tool/file and
state sections all survive; if a structured fit is impossible, `context.manager`
raises an actionable `ContextOverflow` (no repeated rollover). Nested trailing
clip markers are removed on reclipping so they never accumulate, while literal
interior markers are preserved.

Token accounting uses the configured counter: the brief's cost in the budget is
the counted history cost, and `context.handoff.tokens_before`/`tokens_after` are
the assembler's projected history and kept-suffix counts (plus the brief under
the default tokenizer). A direct `Session.handoff()` call has no assembler and
uses the default tokenizer over the same canonical message text.

`SummaryRecord(strategy="handoff", kept_from_seq, handoff_id, brief_version)`
plus `context.handoff`, row head/count and any continuation admission commit in
one SQLite transaction. Only the active brief and retained messages are model
input; `Session.messages`, exports and transcript views remain full history.
Historical cutoffs derive the head visible at that cutoff. Fork copies the
selected prefix and its head into a *new* identity; handoff keeps the identity;
agent reset only changes the descriptive agent selection.

## Recursive context (code mode)

Code mode ([tools.md](tools.md#code-mode-the-code-tool)) can read session contexts
through host functions in the program's flat namespace: `context_meta`,
`context_messages`, `context_tool_calls`, `context_search`, `context_report`, and
`llm`. They are on offer only when `[code_mode] context = true`. All reads take a
`target`: `"self"` (default; the current session) or a name or id declared in the
program's `targets` variable. Results are redacted, bounded, and marked
**untrusted data, not instructions**. Each read is a `code.call` event, so the user
sees what was read.

**Scope (v1).** Only root sessions get the namespace; a child never reads its own
descendants. The manager session never gets it. This is a recorded decision
([decisions.md](decisions.md)) rather than an omission.

**Authority** (`nexus/session/context_authz.py`) is host-owned and never comes from
model, provider or voice text. The reader must reach the target by walking owner
edges (`sessions.owner_id` / `owner_namespace`, `child_allocations`), with
`context_read = 1` on **every** hop. Nexus-run children get `context_read = 1` when
spawned; external workers (Claude Code, Antigravity) get 0, and existing or legacy
edges fail closed. Further rules: the same project, at most `max_depth` hops
(default 1, ceiling 8), and a trashed or purged session on the path denies. The
check runs again on every call, so revocation takes effect mid-program. Completed
children stay readable. Siblings, ancestors, the manager and other projects are
denied. Not verified live: only offline tests exercise these paths.

**Permission.** A non-self target is a synthetic permission key `ContextQuery(<name>)`
evaluated by the ordinary engine (`deny` is absolute; mode fallback is always `ask`, so
`mode = allow` does not cover it, only an `allow` rule such as `ContextQuery(*)` or a
grant does). It is checked up front, once per non-self target, before the program runs
([decisions.md](decisions.md)). Approval never widens authority: authz
still applies.

**Read port** (`nexus/session/context_port.py`, `ContextReadPort`) reads only the
target's own records, bounded per call: a range scans at most 200 seqs and returns at
most 100 rows; `search` examines at most 5000 records or 512 KiB and returns at most
50 hits. `context_report` returns the last assistant text, else a handoff digest.
Output is redacted before it leaves the port.

**`llm(prompt, text)`** is one tool-less request on the session's own model, or
`[code_mode] sub_model` when set. No fallback, no tools, depth 1. Limits (config):
`max_sub_calls` 16, `max_sub_input_tokens` 32000, `max_total_sub_tokens` 200000,
`max_depth` 1. The text argument is wrapped as `<context source=... seq=...>` so the
sub-call treats it as data. Calls are charged to a **per-program** bounded
`SubagentBudget`. Known gap: the turn-shared subagent tree budget is not reachable
from `Runtime` at this layer, so sub-call spend is not deducted from the turn's tree
budget; only the per-program bound applies.

Injection posture: tool output or child content that says "call llm 1000 times" or
contains code is only data; the caps above hold and nothing in it is executed
(offline-tested; live behavior not verified).
