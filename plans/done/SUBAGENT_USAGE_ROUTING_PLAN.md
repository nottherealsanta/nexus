# Live tiers and usage-aware sub-agent routing

Status: steps B-E implemented and tested offline (window kinds, config, usage gate and snapshot service, spawn-time routing, `agent.routed` event and reducer); not verified live. Built as `host_support/usage_snapshot.py` plus `Runtime.route_subagent` (the walk happens in the runner at spawn, not inside `ModelRouter`). Steps A, F (TUI/GPUI rendering), G, H and I (remaining docs) are open. Replaces
`PROVIDER_USAGE_THRESHOLDS_PLAN.md` (deleted), whose fail-closed, per-request
design was narrowed by the decisions in §2.

## 1. Goal

1. **Tier edits apply immediately**, even while a turn runs. No restart.
2. **A sub-agent never starts on a provider whose quota is nearly spent.** When the
   first model of a tier (or fallback chain) belongs to a provider whose remaining
   usage in any window is at or below that window's threshold, the sub-agent is
   created on the next model in the list. This is automatic and needs no restart:
   once usage crosses the line mid-session, the next spawn already routes around it,
   and once the window resets the provider is used again.
3. **The main agent is never rerouted.** When the user selects a main model whose
   provider is below a threshold, a toast says so; clicking it opens the usage panel.

## 2. Decisions (user, 2026-10-15)

| Question | Decision |
| --- | --- |
| Threshold meaning | One **minimum remaining percent per window kind**: 5-hour **10%**, weekly **5%**, monthly **2%**. A provider is blocked when *any* window it reports has `remaining ≤ threshold`. |
| Missing windows | Providers report different windows (some have no 5-hour, some only monthly). Only the windows a provider actually reports are checked; a missing kind is simply not evaluated. |
| Scope | Sub-agents only. Main agent: toast on selection, no rerouting. |
| Unknown usage | Usage that is unavailable, failed, stale, or unparsed counts as **available** (fail open). A broken usage endpoint must never block delegation. |
| All candidates blocked | Use the first candidate anyway and say so; never fail the spawn for quota reasons. |

## 3. Current behaviour (why it does not work today)

- `nexus/host/facade.py:1929`: `ModelTierSet` / `ModelTierReset` /
  `AgentMaxTierSet` only reload when `not self.supervisor.running`. Otherwise
  the config is written, `restart_required=True` is returned, and nothing ever
  applies it later. This is the "needs a restart" behaviour.
- `Runtime.reload_tier_config` (`nexus/runtime.py:6362`) is already a cheap
  in-place swap (tier table, `registry.retier`, `router.set_tiers`, assembler).
  It does not touch providers, so the idle guard copied from
  `reload_model_routes` is unnecessary for tier-only edits.
- A child's model is resolved at spawn (`ModelRouter.resolve` → `_resolve_tier`
  → `tier_candidates`, `nexus/model/router.py:231`), so in-flight children are
  unaffected by a swap.
- `_resolve_tier` takes the first candidate whose provider is registered; it
  never looks at usage. `fallbacks()` lists the next candidates, used only after
  a provider error with no output.
- Usage is fetched on demand by `ProvidersUsage` in
  `nexus/host_support/provider_usage.py` (above `runtime` in the layering), so
  the router cannot reach it directly.
- The sub-agent roster rendered into the prompt reads `tier_efforts` from the
  runner's construction-time config (`nexus/agents/runner.py:1179`); it may stay
  stale after a reload (to verify in step A).

## 4. Design

### 4.1 Live tier edits

- Tier-only commands always call `reload_tier_config`; `restart_required` stays
  `False` unless the runtime returns `False` (injected routes).
- `reload_model_routes` (providers change) keeps its idle guard.
- The roster's tier lines read tiers and efforts from the router (live), not the
  runner's captured config. The roster lists the **configured order**, never the
  live usage-filtered order, so usage changes do not churn the prompt cache. A
  tier edit does change the roster; that cache cost is accepted because the user
  asked for the edit.

### 4.2 Window kinds and thresholds

Usage rows already carry `period_seconds` per window. Add a machine-readable
`kind` to each window in `provider_usage.py`, never inferred from a display label:

- `five_hour`: period ≈ 5 h (Codex primary, Claude session, OpenCode rolling).
- `weekly`: period ≈ 7 d.
- `monthly`: calendar month (Copilot, xAI monthly, OpenCode monthly).
- `other`: anything else (e.g. per-product side quotas); shown, never gated.

Kind is chosen by a bounded tolerance on `period_seconds` (exact rules plus a
fixture per provider in step B). A window with no period and no known fixed
length is `other`.

Config (global `config.toml`, `ModelsSection` in `nexus/config/schema.py`):

```toml
[models.subagent_usage]
enabled = true                 # default true
five_hour_min_remaining = 10   # percent; defaults shown
weekly_min_remaining = 5
monthly_min_remaining = 2
refresh_seconds = 180          # background poll; bounded 60..3600

# Optional per-provider override; absent keys inherit the defaults above.
[models.subagent_usage.providers.github-copilot]
monthly_min_remaining = 10
```

- Values are finite numbers in 0..100; booleans, NaN, infinity, unknown keys and
  out-of-range values are `ConfigError`. `0` blocks only at exhaustion.
- Comparison uses full precision: `100 - used_percent <= min_remaining` blocks.
  Example: weekly min 5, used 95.0 → remaining 5.0 → blocked; used 94.9 → allowed.
- Thresholds are user-global. Workspace config may not lower them (stricter wins
  if a workspace layer sets them).

### 4.3 Usage snapshot service (layering-safe)

- **Host side** (`nexus/host_support/usage_gate.py`, new): a daemon-owned poller
  that calls the existing `providers_usage` fetchers for connected providers every
  `refresh_seconds`, coalesces concurrent refreshes, and also refreshes a provider
  shortly after a sub-agent on it finishes (debounced). `ProvidersUsage` reads the
  same cache when fresh, so the usage panel and routing see the same numbers.
- It pushes a frozen `UsageSnapshot` (provider → windows with `kind`,
  `used_percent`, `resets_at`, `observed_at`) into the runtime via
  `Runtime.set_usage_snapshot`.
- **Model side**: the router receives a structural callable,
  `headroom(provider: str) -> UsageVerdict` (`ok` | `blocked` | `unknown`, plus
  the breaching window kind, remaining percent, threshold and reset time). The
  verdict logic lives in `nexus/model/usage_gate.py` (pure, no I/O, no imports
  above `model/`), so `tests/test_layering.py` stays green.
- Freshness: a window whose `resets_at` has passed is `unknown` (fail open) until
  re-fetched. A snapshot older than `3 × refresh_seconds` is `unknown`.
- Route → quota identity: the provider id of the route, with known aliases mapped
  together (`claude-agent`, `claude-code` workers and SDK backend share Claude's
  quota; `codex` ≡ `openai-codex`). Mapping table lives next to the verdict code
  with a test per alias.

### 4.4 Routing

In `ModelRouter`, for **child requests only** (request metadata marks
sub-agent spawns; root, title and compaction requests never consult `headroom`):

1. Build the candidate chain exactly as today (agent → tier → global fallback,
   role tier clamps, explicit refs).
2. Walk it; skip candidates whose verdict is `blocked`; `ok` and `unknown` are
   eligible. First eligible wins.
3. If every candidate is blocked, use the first runnable candidate and mark the
   decision `all_blocked`.
4. `fallbacks()` applies the same filter, so a provider-error retry also skips
   blocked providers.
5. External workers (`_external_tier_route`, Claude Code / Antigravity launch)
   use the same walk before launch. An already running external CLI is never
   killed or switched.
6. A running Nexus child is **not** switched between its own requests: changing
   provider mid-conversation loses cache and reasoning state. Its next sibling
   spawn routes around the quota instead.

The router returns a small `RouteDecision` (chosen ref, skipped refs with
verdicts, `all_blocked`) alongside `ResolvedModel`.

### 4.5 Showing the decision (durable)

- A typed event recorded in the parent session when a child is created with a
  non-trivial decision, e.g. `subagent.route` with requested tier/ref, chosen
  ref, and each skipped ref with window kind, remaining %, threshold and reset.
  Reducer support in `nexus/view/reduce.py`; replay reproduces it.
- Ratatui and GPUI render it on the sub-agent's row as a labelled line:
  `low → openai-codex/gpt-6-luna · skipped github-copilot/gpt-6-luna (monthly
  1.5% left ≤ 2%, resets Nov 1)`. `all_blocked` reads as a warning.
- Settings → Models: each tier entry shows a live status badge
  (`below threshold: weekly 3% left`) from a new host field on `ModelTiersResult`.
- Usage panel: each window shows its threshold mark and a `reserved for main
  agent` tag when blocked, separate from the existing pacing reserve.
- Settings → Models (or Providers): the three window thresholds plus per-provider
  overrides, edited through new host commands `SubagentUsageSettings` /
  `SubagentUsageSettingsSet`; applied live (no restart).
- The deprecated web client is not extended.

### 4.6 Main-agent toast

- `ModelSelectResult` gains `usage_warning: dict | None` (provider, window kind,
  remaining %, threshold, reset), computed from the same snapshot (no extra fetch;
  unknown → no warning).
- Ratatui: `actions.toast(..., level="warning", action={"label": "Usage",
  "operation": <open usage panel>})`. The toast row's existing action plumbing
  (`rust/tui/src/render/toasts.rs`) opens the panel on click, same as `Ctrl+U`.
- GPUI desktop: same toast + click → usage panel.
- The toast is shown once per (session, provider, window) selection, not on every
  snapshot refresh.

## 5. Implementation sequence

- [ ] **A. Live tier edits.** Drop the idle guard for tier-only commands; make the
  roster read live tiers/efforts. Tests: edit during a running turn → next spawn
  uses the new list; `restart_required` false; roster reflects new efforts.
  Ship on its own.
- [x] **B. Window kinds.** Add `kind` to usage windows for every provider, with
  recorded fixtures (Codex 5 h + weekly, Claude session + weekly, Copilot monthly
  only, OpenCode rolling/weekly/monthly, xAI weekly or monthly, Antigravity).
- [x] **C. Config.** `models.subagent_usage` schema, validation, defaults, merge
  rule (stricter wins), `docs/config.md`.
- [x] **D. Snapshot service + verdict.** `host_support/usage_gate.py` poller and
  cache; `model/usage_gate.py` pure verdict; `ProvidersUsage` reads the shared
  cache; alias → quota identity table.
- [x] **E. Routing.** Child-only filtering in `resolve` / `fallbacks`; external
  worker launch path; `RouteDecision`.
- [ ] **F. Durable decision event** + reducer + Ratatui/GPUI rendering on the
  sub-agent row.
- [ ] **G. Settings and usage panel**: threshold controls, tier badges, threshold
  marks; host commands.
- [ ] **H. Main-agent toast** on `ModelSelect` with click-to-usage in Ratatui and
  GPUI.
- [ ] **I. Docs**: `docs/models.md` (routing, tiers), `docs/agents.md`,
  `docs/config.md`, `docs/host.md` (commands), `docs/surfaces.md`,
  `docs/ratatui-parity.md`, `docs/desktop.md`, `docs/decisions.md` (fail-open,
  spawn-time only, sub-agents only), `docs/module-map.md` rows for new modules.

## 6. Tests and acceptance

Offline, with fake time, a fake usage source and `ScriptedProvider`:

- Thresholds per window kind: equality blocks; just above allows; fractional
  percentages; `0` and `100`; per-provider override; stricter workspace value.
- Providers with only monthly, only weekly + monthly, and all three windows; a
  missing kind is never evaluated.
- Unknown/failed/stale/expired-reset usage → eligible.
- Tier `low` with first provider blocked → child runs on the second; decision event
  recorded and replayed identically.
- All blocked → first candidate used, `all_blocked` shown.
- Provider-error fallback skips blocked providers; agent/tier/global order kept.
- Aliases sharing a quota (Claude worker / SDK / agent) are blocked together.
- Root model, title and compaction requests are never rerouted.
- Usage crossing the threshold mid-session → next spawn reroutes without restart;
  a reset snapshot → provider used again.
- `ModelSelect` for a blocked provider returns `usage_warning`; Ratatui toast has
  an action that opens the usage panel (PTY check).
- Layering, UI-layering and docs tests pass; relevant Rust tests pass.

Acceptance: with the defaults, Copilot at 98.5% monthly used and tier `low`
listing Copilot first, a new sub-agent starts on the next listed model, the
timeline says why, the main agent still runs on Copilot when selected (with a
toast), and after the monthly reset the next sub-agent uses Copilot again, all
without a daemon restart.

Not verified live: OpenCode Go and Antigravity usage formats (see
`provider_usage.py`); they fail open if parsing fails.
