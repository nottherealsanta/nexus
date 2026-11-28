# 55 · Direct Claude subscription mode

Requested 2026-11-28. Separate from the existing Agent SDK mode; Nexus controls native Messages and tools.

## Implemented and checked

- [x] Inspect OMP, cloned to `/tmp/nexus-omp-reference-20261128`, revision
  `499544713fc7ee7dcf9e7ffcc45863fa57d9adca` (`can1357/oh-my-pi`). Its bearer-token transport is separate from
  Claude Code identity spoofing (user-agent/runtime headers, official-CLI system instruction, tool prefix).
- [x] `claude-direct`, native history/images/tools and live SSE, signed reasoning with generating-model provenance.
- [x] Remove redundant `Direct ·` model-name prefix (user, 2026-11-28); both clients retain the `claude-direct`
  provider label, and the shared catalog/scenario supplies the plain model name. Validation: 77 Core tests, both
  TUI direct tests and both NSH direct tests passed; targeted Ruff passed. Composer text is asserted in both clients;
  paired screenshots were not captured for this label-only change.
- [x] Model-specific budget/adaptive/effort compatibility; catalog output ceilings, 64000 unknown-model fallback,
  4000-token visible-answer reserve for budget thinking. Other-model policies are offline-tested, not live-verified.
- [x] One-hour caching by default, short/disabled adapter options, at most four tool/system/rolling/every-15-user-turn
  breakpoints; no thinking decoration or raw mutation. Actual Haiku cache hit verified.
- [x] Nexus-owned browser PKCE and process/file-locked refresh, stored in owner-only `auth.json`; explicit environment
  token then owned login then selected CLI-profile token. CLI tokens stay read-only; no Keychain read or shared-token rotation.
- [x] Direct subscription usage uses the same inference credential. Native image estimates and generation-option exclusion.
- [x] Object-root tool schemas, root combinator constraints moved into descriptions; local tool validation unchanged.
- [x] Live Haiku text/native tool roundtrip, thinking/signed tool replay, user/tool images, cache hit, and actual Nexus
  `read` tool loop with all default tool schemas offered. The full-schema probe initially exposed Anthropic's root `anyOf`
  rejection on `edit`; normalization fixed it and the live loop passed.
- [x] Offline auth/PKCE/concurrent refresh, replay, streaming, redaction, HTTP errors, usage, catalog and routing tests.
- [x] Shared `interface/scenarios/claude-direct.json` consumed by TUI/NSH tests and the scripted visual Core.
- [x] Client tests and paired provider-detail screenshots inspected; full note is visible with matching wording.
  TUI PNG: `/tmp/nexus-direct-visual.nNd28x/out/direct-provider.png`; NSH artifact: `art_1a21ed0e8483a7d0fb2`.
  Installed NSH revision mismatch resolved by building the already-cached pinned revision into
  `/tmp/nexus-direct-nsh-pinned-build`, without changing the repository pin. Web builds use rustup's `RUSTC` because
  Homebrew's rustc lacks the WASM target. NSH package build, direct-provider test and Settings keys tests pass.

## Remaining functional parity with OMP

- [x] Fresh browser OAuth authorization and owned-token refresh live test: user completed sign-in into an isolated scratch
  home; all five Haiku probes passed again, then refresh and eager-native-tool roundtrip passed. CLI credentials untouched.
- [x] Recursive wire-only schema normalization and root object-union folding; literals/property names preserved,
  open maps not narrowed, local validation unchanged. Actual Nexus Haiku loop with default schemas passed again.
- [x] Optional selective eager tool input streaming (`eager_tools` adapter option), Haiku probe live-verified.
- [x] Per-request cache retention override (`anthropic.DirectRequest.cache_retention`), final-body-derived extended-TTL headers; offline-tested.
- [x] Model-specific fail-closed prefix binding for supported generations; no automatic reasoning drop/retry.
  Offline-tested only; supported models were not approved for live calls.
- [x] Preserve bounded token-response identity, scope and authorization time, with identity stable on refresh.
- [x] Unknown/mismatched content deltas and unknown events fail explicitly rather than silently changing context.
- [x] Optional direct-only long-lived browser grant: narrow profile/inference scopes, requested one-year access lifetime,
  server expiry honored, no refresh token retained. Offline PKCE/token/no-refresh tests pass; fresh live grant unverified.
- [x] Isolation hardening requested by user: shared `Request` unchanged; independent direct auth never starts CLI
  login/logout; read-only `cli-profile` method distinct from SDK's unchanged `cli`. Existing provider routing/catalog
  regression tests pass; native image/generation estimation restricted to direct-shaped Messages bodies.
- [x] Direct credentials/refresh locks separated into `claude-direct/auth.json`; CLI profiles require a token-free
  profile-bound opt-in marker, so existing SDK/CLI users do not auto-connect or get direct background usage requests.
  Malformed direct credentials fail closed during discovery. Lock-independence and discovery regression tests pass.
- [ ] Adaptive models and long-context live checks (user requested Haiku only, so no other-model calls yet).
- [ ] Long-lived grant live verification, account/org identity display and multiple-account selection/affinity. Returned identity is
  retained internally, not displayed; no bootstrap identity fallback or cross-account quota pooling.
- [x] Optional selective strict decoding (`strict_tools`): compatible closed schemas only, request-wide 20/24/16
  tool/optional/union budgets, no local schema mutation or OMP name allowlist. Overflow and eager tools remain
  non-strict; no nullable-required injection or hidden retry. Native strict Haiku roundtrip live-verified; complex schema cases offline.
- [x] Direct request-size/image bounds (128 MB; 100 pre-4.6/unknown, 600 recognized 4.6+), fail-before-send with no
  context removal. Explicit legacy/new API-version selection and deduplicated final-body-derived beta headers; new-version Haiku text live-verified.
- [x] Explicit provider-layer prompt-cache prewarm (`DirectRequest.prewarm`): one visible zero-output request,
  bounded raw response, usage accounted; unexpected content rejected, budget thinking/no cache rejected before send.
  Haiku zero-output prewarm followed by a cache hit live-verified; automatic scheduling remains open.
- [x] Explicit native tool choice/forced tool in `DirectRequest`; unknown/unoffered choices and incompatible thinking,
  model or prewarm combinations fail before authentication instead of silent option changes. Offline-tested only.
- [x] Legacy version-first and separator-collapsed Claude ids share canonical thinking/image policies; malformed numeric
  suffixes fail closed. Catalog/routing and alias tests pass; no shared SDK catalog policy changes.
- [x] Explicit stable/volatile system cache boundary (`DirectRequest.stable_system_chars`): wire blocks concatenate
  to the exact original instructions, cache head survives suffix changes, no inferred markup or context reordering.
  Zero means no stable system prefix; default unchanged. Offline-tested; automatic context scheduling remains open.
- [x] Optional gzip request compression: diagnostics retain the original body, uncompressed limit enforced, no hidden
  encoding fallback. Haiku text probe live-verified; prewarm/compression combinations offline-tested.
- [x] Native token task-budget controls, validated immutable direct-only values, effort merged, signed-replay remaining
  override rejected rather than dropped. Haiku 4.5 returned HTTP 400; now restricted locally to known adaptive models.
  Adaptive acceptance unverified; no automatic session budget accounting.
- [x] Stream integrity: duplicate starts/reused indices, overwritten initial tool input, bad usage/conflicting stops fail;
  usage-only deltas preserve stop reason, missing initial message cannot succeed. Exact nonstandard stop reason retained.
- [x] OAuth refresh refuses conflicting returned account/org UUID without replacing stored identity or tokens; offline-tested.
- [ ] Deferred tool loading needs durable active-roster/control records, not merely a `defer_loading` flag.
- [ ] Volatile-context scheduling and learned endpoint cache compatibility. No hidden retry that strips cache/context
  options; per-request retention and prewarm are implemented at the provider layer only.
- [ ] Live prefix-binding checks and explicit visible cross-model reasoning transformations; cross-model replay currently drops signed reasoning.
- [ ] Server-side compaction/threads/tool changes. Nexus's visible context rules apply; unsupported content blocks fail explicitly.
- [ ] Cache-write pricing ledger (normalized usage currently includes writes in total input but has no separate cache-write field).

## Continuation validation (2026-11-28)

- [x] OMP 429 follow-up: cloned current reference to `/tmp/nexus-omp-429-reference-20261128`, revision
  `c9730fdb620ce4599dd898691e3e3b56003bfcb5`. Beta URL/utility headers and full CLI headers alone still 429;
  OMP system/billing identity with CLI headers succeeded on Sonnet/Haiku, and with Nexus headers succeeded on Sonnet.
  The official-CLI instruction **alone** succeeded with Nexus headers and legacy URL/version. User explicitly approved
  permanent visible compatibility, superseding the earlier no-identity-instruction decision. Adopt only that instruction:
  no billing hash, runtime impersonation, tool renaming, extra requests, fallback credits or account pooling.
  Both clients' System prompt uses the exact provider-prefixed prompt via display copies, including draft/open/snapshot
  endpoints; frozen snapshots remain unchanged. Real Nexus Sonnet 5.5 medium-effort `read` loop passed (two HTTP 200s,
  native tool executed, exact `NEXUS_SONNET_DIRECT_OK`, normal two-step finish); no signed reasoning was returned.
  The unchanged default 128000-token output cap also passed a separate short Sonnet medium response (HTTP 200).
  238 Core integration tests, three TUI tests and three NSH tests passed; full Core Ruff and diff checks passed.
  The new live Sonnet regression is separately quota-gated (not automatically run with the Haiku suite).
  Paired screenshots were not captured; both clients' exact System prompt detail is covered by focused tests.
- [x] Diagnose reported Sonnet 5.5 HTTP 429: original three attempts had no retry timing; two user-approved minimal
  128-token requests (legacy/new API versions, no tools/thinking/cache) both returned `rate_limit_error` with only
  `Error` as the service message and no `Retry-After`. This rules out the large output cap/tool/cache/thinking body
  as necessary causes, but does not establish quota exhaustion versus a service-side restriction. No provider fallback
  or identity spoofing added. HTTP diagnostics now use status-specific wording and bounded, allowlisted error types
  without retaining service messages or raw bodies. Sonnet inference remained blocked at this checkpoint; the OMP
  follow-up above subsequently resolved the reproduced failure.
  Validation: 80 focused tests and 215 provider/policy/auth/catalog/routing integration tests passed; targeted Ruff
  and `git diff --check` passed. Both clients consume the same Core error string; no client code changed, client suites
  and paired screenshots were not run for this diagnostic-only change.
- 47 focused offline tests passed for the first compatibility tranche; 223 integration tests passed (5 live probes skipped), Ruff passed.
- After per-request retention/mismatched-delta additions: 79 focused schema/provider/JSON/counting tests passed, Ruff passed.
- Live: five Haiku probes passed using fresh owned OAuth; two targeted checks passed for owned refresh and selective eager
  native-tool roundtrip. The initial live attempt failed locally before HTTP because no usable selected credential was available.
- No client layout/wording/interactions changed in this continuation; earlier paired provider screenshots remain the client evidence.
- Subsequent isolation/long-lived-grant tranche: 71 focused tests and 297 provider/SDK/worker/auth/server integration tests
  passed; Ruff passed. Direct TUI test (1) and NSH tests (2) passed; NSH pinned-toolchain package build passed without
  changing its pin. Paired disconnected-provider sign-in screenshots inspected: TUI
  `/tmp/nexus-direct-isolation-shots/direct-signin.png`, NSH `art_1a220474d67735f6ecb`; all three direct sign-in choices
  have matching wording/order, with no client-specific difference added.
- Strict/prewarm/request-limit tranche: 108 focused offline tests passed, then 386 provider/auth/SDK/worker/loop/
  server integration tests passed (6 live checks skipped); full Core Ruff and `git diff --check` passed.
- Native tool-choice and model-alias follow-up: 140 focused/provider/catalog/routing tests passed, 9 live checks
  skipped; full Core Ruff and diff whitespace checks passed. An alias edge-case regression exposed a malformed
  numeric suffix being parsed as a major-only model; the parser was corrected and the focused checks passed.
- User approved five bounded Haiku requests (strict roundtrip, prewarm/cache hit, new API version), but the prior
  scratch login path was unavailable. User chose to leave live checks pending rather than sign in again; no real
  provider request was made during this continuation. The three new live probes remain explicit opt-in tests.
- Stable/volatile cache-boundary follow-up: 150 focused/provider/catalog/routing tests passed, 9 live probes skipped;
  full Core Ruff and `git diff --check` passed. No client UI or shared request/auth code changed in this continuation.
- User then signed in again into an isolated scratch home. Three new live probes passed (five requests): strict
  native-tool roundtrip, zero-output prewarm/cache hit and API version 2026-10-21 Haiku text. Gzip Haiku text also
  passed; task-budget Haiku text returned HTTP 400 with no retry/downgrade. Local adaptive-only guard added afterward.
  Scratch auth home: `/var/folders/qf/xhn4kdq53z19x2sbpt0fw7jm0000gn/T/nexus-claude-direct-live-20261128-0r6b1l_0`.
- This tranche: 474 provider/auth/SDK/worker/loop/server integration tests passed, then 204 focused tests passed
  (11 live checks skipped before removing the local-only task-budget case from the live suite); final targeted
  cleanup: 131 passed, 10 live probes skipped. Full Core Ruff and diff whitespace checks passed. No client/shared Request changes.

No claim of undetectability, official support or subscription-terms compliance. The user approved the visible official-CLI
compatibility instruction on 2026-11-28; HTTP/runtime/billing spoofing, account-ban evasion, credit fallback and
cross-account quota pooling remain excluded. Existing SDK mode remains unchanged.
