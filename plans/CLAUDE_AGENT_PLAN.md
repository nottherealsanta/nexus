# Claude Agent SDK provider

## Goal and boundary

Add `claude-agent` alongside the Anthropic API provider, using the official
`anthropics/claude-agent-sdk-python` package and Claude Code's subscription login.
Nexus never reads, exports, or stores Claude OAuth tokens. The SDK CLI owns auth.
Before querying, its bounded `auth status --json` must report a Claude.ai login;
API-key logins are refused so this route cannot silently select API billing.

Anthropic's June 16, 2026 update says SDK, `claude -p`, and third-party usage still
counts against subscription limits. This is provider-controlled and can change:
https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan

## Implementation

1. Ship a pinned optional `claude-agent` dependency extra. Import the SDK only in
   a worker, so installations without this extra retain existing functionality.
2. Run each model request in an isolated Python worker with an OS-only environment.
   No parent API keys, OAuth token variables, cloud switches, or arbitrary Python
   import paths enter the worker. The parent bounds input/output and the overall
   request deadline and kills the worker process group on close or cancellation.
3. Disable SDK built-in tools, settings sources, hooks, external MCP discovery,
   and SDK session persistence. The SDK structured response contains prose and
   Nexus tool intentions. Validate the whole batch, then emit ordinary Nexus
   tool-call events; the existing loop persists, approves, and executes them.
4. Reconstruct every request from Nexus's assembled history. Each SDK request is
   independent, so forks, compaction, replay, and reconnect do not depend on an
   SDK session id. Preserve tool results while omitting harness-only metadata.
5. Route configured `claude-agent` models to this adapter. Project Anthropic
   catalogue entries into this provider for `/model` and setup, masking API-only
   vision, thinking, structured-output, temperature, and pricing claims.
6. Expose subscription detection and connection instructions through shared host
   setup data. Both TUI and browser already render those rows and use SetupSave.

## Deliberate limits

This bridge is text-only and buffers the final structured response; it does not
stream incremental prose or expose private thinking. SDK output has a bounded
32-call batch and JSON argument strings. Nexus's existing schema/path/permission
checks remain authoritative. Sampling temperature, top-p, stop sequences,
explicit thinking budgets, and exact output-token caps are not mapped to SDK
options. Supported low/medium/high/xhigh/max reasoning effort is passed through.
SDK calls may use additional model turns to produce structured output. Usage
includes all SDK turns, including cache reads/writes; catalogue API prices do
not represent subscription charges. Subscription allowance and available models
are enforced by Anthropic, not guaranteed by the catalogue.

## Validation and release

Offline tests cover worker options, tool translation and rejection, metadata
filtering, text-only rejection, usage, sanitized environments, timeout/cancel
cleanup, authentication gating, setup, and catalogue routing. Run the full offline
suite and Ruff before creating the change PR. Merge after required checks, inspect
and merge the generated 0.2.3 patch release PR, and verify GitHub and PyPI assets.
Live subscription inference requires an authenticated account and is not covered
by the offline suite.
