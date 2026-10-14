# Provider usage thresholds for sub-agent routing

Status: proposed; not implemented or verified.

## 1. Goal and scope

Let users reserve provider quota for themselves and the main agent by configuring
minimum remaining usage for each provider. Once a configured threshold is reached,
**no sub-agent model using that provider's quota may start another request**. The
sub-agent uses the next eligible model in its existing ordered fallback chain.

This policy applies to all descendants, all roles, all tiers, explicitly selected
models, SDK children, and external Claude Code workers. It does not change main-agent
routing, title generation, or other background model calls in this first release.
It does not create a new model ranking: “next best” means the next eligible candidate
in Nexus's existing routing order, subject to existing tier and authority limits.

“Session” means the provider's short/rolling quota window (for example Claude's or
Codex's five-hour window), **not** a Nexus conversation or a locally tracked token
budget. Weekly and monthly windows also use provider-reported usage and reset times.

## 2. Current behavior

- `nexus/host_support/provider_usage.py` fetches usage for the `ProvidersUsage` host
  command. Windows carry percentages, periods, reset information, and labels.
- `nexus/ui_support/usage.py` formats warning colors and pacing reserve. These are
  presentation only; pacing reserve is not the quota reserve proposed here.
- `nexus/model/router.py` resolves runnable models without checking plan usage.
- `nexus/core/loop.py` falls back after a provider error with no output, not after
  observing a usage percentage. Refusals and partial streams do not fall back.
- External Claude Code workers have a separate launch path in `nexus/runtime.py`;
  guarding only the normal router would leave a bypass.

References: `docs/models.md`, `docs/agents.md`, `docs/config.md`, `docs/loop.md`.

## 3. Configuration contract

Proposed syntax (names to be finalized against the existing schema before coding):

```toml
[providers.openai-codex.subagent_usage]
session_min_remaining_percent = 10
weekly_min_remaining_percent = 20

[providers.github-copilot.subagent_usage]
monthly_min_remaining_percent = 15

[providers.claude-agent.subagent_usage]
weekly_min_remaining_percent = 25
```

- Each field is optional. Absent means disabled for that window; no fields means
  no usage gating and preserves current behavior.
- Values are finite numbers from 0 through 100, inclusive. Booleans, unknown keys,
  out-of-range values, NaN, and infinity are validation errors.
- A threshold of 0 blocks at exhaustion; 100 always reserves the entire window.
- Users may enable one or several windows per provider. **Any** configured window
  reaching its threshold blocks the provider for sub-agents.
- Thresholds belong to the user's global provider configuration. Settings writes
  global configuration, not project files. Workspace/session layers must not weaken
  a global reserve; define and test an explicit merge rule that retains the stricter
  threshold (higher minimum remaining percent) if local tightening is supported.
- Disabled is distinct from 0. UI controls must expose both explicitly.

Example: weekly minimum remaining = 20%, reported used = 80%, remaining = 20%:
block. At 79% used, allow. Compare full-precision normalized values, not rounded
UI strings: `remaining_percent <= minimum_remaining_percent` blocks.

## 4. Stable quota identity and windows

Add machine-readable window metadata to the usage snapshot; never infer routing
policy from localized display labels.

- Normalize window kind to `session`, `weekly`, or `monthly`; retain other windows
  for display without treating them as one of these kinds.
- Retain a stable source/window ID, applicability (provider-wide or model/product
  specific), used percent, reset time, period, and observation time.
- Map configured routes to a canonical quota identity. Aliases sharing credentials
  and quota must not bypass a block by choosing another name or backend. In
  particular, assess `claude-agent`, `claude-code` workers, and SDK routes together.
  Separate accounts must not be conflated merely because their provider kind matches.
- Do not apply unrelated product windows (such as code review or unmetered chat)
  to inference. For applicable model-specific windows, gate the relevant candidate;
  a provider-wide window blocks every model sharing that quota.
- If several applicable windows of the same kind exist, any breach blocks.
- Reset times are authoritative. Expired snapshots require refresh; never assume
  a reset succeeded or continue using a pre-reset percentage as current truth.

Audit every adapter's mapping using recorded fixtures. Unsupported requested windows
must appear clearly as unavailable, not as 0% used or unlimited.

## 5. Shared usage service and freshness

Move reusable fetching/normalization behind a daemon-owned service accessible to
runtime and host through appropriate contracts. Core/model code must not import
`host_support`, perform authentication, or call usage endpoints directly.

- Inject an eligibility protocol into routing/request execution; supply its concrete
  implementation from runtime. Keep the lower layers independent of host/UI code.
- Cache normalized snapshots by quota identity, share them across children, and
  coalesce concurrent refreshes. `/usage` and routing consume the same observations.
- Proposed freshness: 60 seconds; bounded refresh timeout of 40 seconds (matching
  current fetch limits). Refresh only quota identities needed by enabled policies,
  with bounded concurrency. Use monotonic cache age and wall-clock reset timestamps.
- Evaluate snapshots immediately before each child provider request. Refresh on
  expiry and invalidate on relevant configuration/account changes and quota errors.
- A configured policy with unavailable, stale-after-failed-refresh, or unsupported
  usage is **unknown and ineligible** for sub-agents. Try another eligible provider;
  do not silently spend reserved quota. Display the exact reason and how to disable
  or correct the policy. Providers without configured policies remain unaffected.
- Cache gate state, not just percentages, so parallel children see consistent known
  blocks. This is a best-effort quota guard, not a hard spending reservation: requests
  already in flight and provider reporting delays can cross the threshold.

## 6. Routing and execution

1. Build the existing ordered candidate chain, preserving role restrictions, tier
   ceilings, explicit-model handling, and agent/global fallback precedence.
2. Evaluate usage eligibility for each candidate; record skipped candidates and
   their reasons. Apply it again to all fallback attempts, not just initial selection.
3. Choose the first eligible candidate. Explicitly naming a blocked model does not
   bypass policy; use authorized fallbacks if present, otherwise fail clearly.
4. If no candidate is eligible, return a structured, redacted
   `subagent_provider_usage_unavailable` error with blocked/unknown reasons and reset
   information. Never silently inherit the main model or escalate authority/tier.
5. Recheck at each subsequent provider request in a running child. On a newly blocked
   provider, choose the next eligible compatible route using existing request/context
   adaptation. Never replay a partially emitted stream or completed tool actions.
6. If a backend cannot safely switch mid-child, stop at a safe request boundary with
   a structured reason; do not restart the assignment and duplicate its effects.

External workers: check before launch and every retry. An already-running external
CLI controls its own internal requests, so Nexus cannot enforce per-request checks
there. Do not abruptly kill or relaunch it solely on a usage refresh. Show this
limitation explicitly; the guarantee for that backend is launch-time gating only.
SDK children using Nexus's loop receive the full per-request guard.

Provider errors retain today's refusal/partial-stream rules. Usage gating is a
pre-request eligibility decision, not a new reason to replay output.

## 7. User-facing controls and durable explanation

Add host commands/results for reading and updating policy, capabilities, and gate
status; surfaces never access configuration, credentials, or managers directly.

Settings → Providers, per provider:

- Optional session/weekly/monthly minimum remaining controls, labelled in percent.
- Current used and remaining percentages, selected threshold, observation time,
  reset time, and `Eligible`, `Reserved for main agent`, or `Usage unavailable`.
- Unsupported windows explicitly labelled; explain session-window semantics.
- Shared-quota aliases shown together so users understand the scope of a block.

In `/usage`, show the reserve threshold separately from the existing **pacing
reserve**. In the child timeline/details, show skipped provider/model, quota window,
remaining percentage, threshold, reason, reset, and selected replacement. Never dump
raw JSON or silently change models.

Persist routing decisions through the durable event log before showing them. Reuse
an existing event only if its contract genuinely fits; otherwise add a typed event,
reducer support, and replay coverage. Expose status through the host to Ratatui and
GPUI. Do not extend the deprecated browser UI for this new feature.

## 8. Implementation sequence

- [ ] A. Audit quota identities, usage sources, window applicability, external worker
  routing, and configuration precedence. Capture ambiguous mappings in fixtures.
- [ ] B. Add validated configuration and stable normalized snapshot contracts.
- [ ] C. Introduce the daemon-owned cached usage service and injected eligibility
  protocol; migrate `ProvidersUsage` to the shared source without changing display.
- [ ] D. Gate normal child selection, each request/fallback, nested children, and
  external worker launch/retry. Handle exhaustion and unknown usage explicitly.
- [ ] E. Add durable routing explanations and host policy/status commands.
- [ ] F. Add provider Settings controls and usage/status presentation in native clients.
- [ ] G. Update `docs/models.md`, `docs/agents.md`, `docs/config.md`, `docs/loop.md`,
  `docs/host.md`, relevant native-client docs, and `docs/decisions.md`. Register any
  new modules in `docs/module-map.md`.

## 9. Verification and acceptance criteria

Offline tests with fake time, fake usage adapters, and scripted model providers:

- Boundary comparisons for 0, 100, fractional percentages, equality, and rounding.
- Disabled versus 0; each window alone; multiple windows and multiple providers.
- All models sharing a blocked quota are skipped, including aliases and explicit
  selections; separate accounts and unrelated products are not accidentally blocked.
- Tier/role/global fallback order and authority limits remain intact. Every fallback
  attempt is gated. Main-agent requests are unchanged.
- Nested children, SDK routes, external launch/retry, and no-candidate outcomes.
- Mid-child threshold crossings without duplicate streams, tool actions, or tasks.
- Missing windows, invalid responses, unknown percentages, failures, expired/reset
  snapshots, account changes, cache TTL, concurrent refresh coalescing, and cancellation.
- Configuration writes/merges cannot silently lower a protected global threshold.
- Durable decision events replay to the same explanations; credentials are redacted.
- Host commands and native Settings controls round-trip; unavailable states are readable.

Run targeted Python tests, layering/UI-layering/docs checks, relevant Rust tests,
then the existing native PTY check for Settings/routing disclosure. Full offline
suite before completion. Live quota endpoint verification is optional and must be
reported separately; never spend tokens to deliberately exhaust a real account.

Acceptance: with a weekly 20% minimum and 80% reported usage, every new sub-agent
request sharing that quota skips it, picks the next authorized eligible model, and
explains why. The main agent remains free to use it. A fresh post-reset snapshot
above the minimum makes it eligible again without a daemon restart.
