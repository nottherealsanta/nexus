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

## Parts

Rendered in a fixed order (`builtin_parts()`); the number is the budget priority
(0 = required, never dropped).

| Part | Priority | Content |
| --- | --- | --- |
| `identity` | 0 | `IDENTITY_PREAMBLE`, currently empty: the role comes from the agent prompt |
| `soul` | 0 | `SOUL.md` plus the selected agent's prompt (`append_agent_prompt`) |
| `environment` | 1 | workspace, platform, profile, Git branch and status (`capture_environment`; capped by `context.limits.environment`) |
| `tools` | 0 | structured schemas on `ModelRequest.tools`; never in system text |
| `agents_md` | 1 | workspace `AGENTS.md` (`context.limits.agents_md`) |
| `skills_index` | 1 | sanitised `name: description` lines only; bodies load via the `skill` tool |
| `mcp_index` | 2 | bounded, untrusted-data index of servers/roots/resources (≤ `MCP_INDEX_MAX_CHARS`) |
| `memory` | 1 | `MEMORY.md` (`context.limits.memory`) |
| `attachments` | 2 | placeholder (no-op today) |
| `history` | 3 | contiguous suffix of messages; compacts itself to fit |
| `user` | 0 | the current user turn |

`SOUL.md`/`MEMORY.md`/`AGENTS.md` resolve through `resolve_within` (escapes fail
closed; oversized files are a named error, `max_file_bytes` default from `[ext]`).

## Budget

```
input_budget = min(context.max_tokens, model context window)
               − effective_max_output − safety_margin
               (and ≤ model input limit − safety_margin when the catalogue states one)
```

An unknown (≤ 0) capability ceiling is ignored. Priority-0 parts are reserved
first; if they alone exceed the budget the turn fails with `ContextOverflow`
naming every oversized part and its tokens. Lower priorities are granted in
ascending priority then assembly order under their configured caps; `history`
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

## Per-turn freeze and inspection

`ContextManager.for_turn(...)` snapshots config, environment and the rendered
system files once, so every iteration of a turn sees identical inputs; a config
edit affects only the next turn. `Runtime.inspect_context` builds a read-only
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
