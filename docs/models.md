# Models, providers and sign-in

`nexus/model/` (L1) is provider-neutral: message IR, the `Provider` protocol,
adapters, the router, the models.dev registry and tiers. `nexus/auth/` holds
keychain-backed sign-in. To add or change a wire dialect, follow
[provider-onboarding.md](provider-onboarding.md) as well.

## Files

| File | Owns |
| --- | --- |
| `message.py` | IR: `Text`, `Thinking`, `Image`, `Document`, `ToolUse`, `ToolResult`, `MessageMeta` (harness-only, never sent), `Message` |
| `request.py` | `ModelRequest`, `SamplingParams`, `ToolSchema`, `REASONING_EFFORTS` |
| `provider.py` | `Provider` protocol, `ResolvedModel(provider, model, capabilities)`, shared provider errors |
| `capabilities.py` | `Capabilities`, `CapabilityRejected`; `overridden_by` layers registry fields over an adapter's |
| `stream.py` | normalized stream events and `ToolCallAccumulator` |
| `http.py` | `HTTPTransport` (pooling, `RetryPolicy`), `SSEDecoder` |
| `router.py` | `ModelRouter`: reference → `ResolvedModel`; lists fallbacks |
| `registry.py` | `ModelRegistry` over models.dev; `ModelInfo`, `Cost`, catalogue parsing |
| `tiers.py` | `TierTable`, `parse_reference`, `clamp`, `resolve_tier` |
| `selection.py`, `reasoning_effort.py` | durable per-session `/model` and effort selections |
| `tokenizer.py` | `HeuristicTokenizer` (~3.7 chars/token prose, ~2.9 code/JSON) |
| `providers/*.py` | adapters (below) |
| `data/models.min.json`, `data/NOTICE` | vendored offline catalogue and its MIT attribution |

## Message IR rules

- No `system` role; system text is assembled by `context/` and passed on
  `ModelRequest.system`; each adapter places it where its API wants it.
- `Thinking.signature` is opaque, replayed verbatim where the provider needs it
  (Anthropic multi-turn thinking), and never displayed.
- `ToolResult.content` is a block list (tools return images; MCP returns mixed
  content).
- Switching provider mid-session is allowed, lossy and visible: each adapter
  declares a degradation policy (`drop`/`to_text`/`error`) and the loop emits
  `context.degraded`.

## Providers

`[providers.<name>]` selects an adapter (`Runtime._adapter_kind`): explicit
`kind`, then the name table, then the OpenAI-compatible fallback for any vendor
with a `base_url`.

| Adapter (`providers/`) | Names / kinds | Notes |
| --- | --- | --- |
| `anthropic.py` | `anthropic` | Messages API; `count_tokens` endpoint; `usage_input_excludes_cache` |
| `openai.py` | `openai`, `codex`, any `kind = "openai_compatible"` | Responses and Chat Completions behind one class (`api = "responses"\|"chat"`); `EndpointFallback` remembers per-model dialects |
| `gemini.py` | `google`, `gemini`, `kind = "google"` | `generateContent` streaming; synthetic call ids |
| `ollama.py` | `ollama` | native `/api/chat` or OpenAI-compatible mode; keyless, loopback default `http://localhost:11434`; conservative capabilities (`tools = false` unless the registry says otherwise) |
| `opencode.py` | `opencode` (`kind` `acp`) | OpenCode over its Agent Client Protocol subprocess only; ACP tool calls stay inside that agent; child env is an explicit allowlist |
| `claude_agent.py` (+ `_claude_agent_worker.py`, `claude_agent_auth.py`) | `claude-agent` | included in every install; text-only, buffers replies; SDK built-ins, hooks, settings and persistence disabled; Nexus schemas registered through an in-process MCP intention bridge |
| `scripted.py` | tests | deterministic offline provider ([testing.md](testing.md)) |
| `discovery.py` | `.agents/providers/*.py`, `~/.nexus/providers/*.py` | quarantined file providers (`PROVIDER`, `PROVIDERS` or `build(context)`); a configured section always wins a same-named file; legacy `.nexus/providers/` is a lower-precedence fallback |
| `devtools/mock/provider.py` | dev mode | `mock/<scenario>` models ([devtools.md](devtools.md)) |

`ProviderSection` keys: `kind`, `api_key`, `base_url`, `api`, `auth`
(`chatgpt_oauth` \| `github_copilot` \| `keychain`), `profile`, `executable`,
`timeout_seconds`, `command`, `args`, `permission_policy`, `inherit_env`, `env`.
A `base_url` must be http(s); plain `http` only for loopback; any `user:pass@`
userinfo is always redacted. An OpenAI-compatible vendor **must** have a
`base_url`, otherwise its model id and key could go to `api.openai.com`
(a hard `ConfigError`).

## Routing (`router.py`)

Resolution order, first hit wins:

1. config aliases (`default`, `fast`, `plan`, explicit aliases);
2. a **tier name** (`low`/`medium`/`high` or a custom `[models.tiers]` name): the
   first selectable model whose provider the router can stream;
3. a registry reference: `provider/model`, a bare id, or an aggregator alias;
4. adapter fallback: split `provider/model` and use the adapter's own capabilities.

A tier name works anywhere a model string does. `ModelRouter.fallbacks` only
*lists* the `model.fallback` chain; the loop decides when to try one
([loop.md](loop.md#failure-handling)). Per-session `/model` and effort choices
are durable (`model.selected`, `reasoning_effort.selected`).

## Registry and tiers

- `ModelRegistry` is data and lookup only; it does no I/O during a turn.
  Acquisition is fetch-on-first-use with a TTL (`models.refresh_ttl_days`, 7);
  on failure it falls back to a valid stale cache, then the vendored snapshot,
  then empty. `models.offline = true` never fetches. `ModelsRefresh` forces one.
- Only descriptive fields are read (`id`, `name`, `env`, `npm`, `modalities`,
  `cost`, `limit`). A `base_url` or `api_key` in catalogue JSON is ignored:
  **the catalogue can never redirect a request or supply a credential.** `env`
  is a name, never read as a value. A catalogue marked `_license: "pending"` sets
  `license_pending` rather than being trusted.
- Host model list, detail and tier reads initialize the catalogue automatically,
  using the same cache-first, single-flight load as turns. Enabled Claude Agent
  models are available after daemon restarts without repeating `nexus claude init`;
  initialization does not change the default model or start a sign-in.
- **Tiered pricing.** `Cost` carries `tiers: tuple[CostTier, ...]` (ascending by
  `context`, max 8) from models.dev `cost.tiers` entries of `tier.type ==
  "context"`; a tier's rates apply when the prompt exceeds its `context` size
  (the highest such tier wins, else the base rates). The legacy
  `context_over_200k` key becomes a tier at 200000 only when `tiers` is absent.
  Malformed or non-context tiers are skipped, never failing the catalogue.
  Runtime cost accounting (`Runtime._child_cost`) prices a turn by prompt size
  = input + cache read + cache write tokens. `Cost.pricing()` is the JSON form
  and reaches the context manager as `Capabilities.pricing` (informational, not
  a provider claim; the registry value overrides the adapter's when present).
- The registry is authoritative for capabilities it describes (`tools`,
  `thinking`, `json_schema_strict`, `vision`, `documents`, limits);
  transport-only fields (`parallel_tool_calls`, `prompt_caching`, `streaming`)
  stay with the adapter.
- Tier resolution, deterministic: `[models.tiers]` user pin → curated map shipped
  with Nexus → blended cost `input + output/4` (`low ≤ 2.5 < medium ≤ 10 < high`)
  → no cost data reads as `low`. `clamp` (used for `agents.max_tier`) only ever
  narrows.

## Authentication

| Route | Code | Storage |
| --- | --- | --- |
| API key (`${env:NAME}` reference) | adapter at request time | never stored by Nexus |
| ChatGPT / Codex OAuth (`auth = "chatgpt_oauth"`) | `auth/codex.py`: browser PKCE or device code; protocol pinned to a cited OpenCode commit (see `auth/NOTICE`) | rotating refresh token + routing metadata in the native keychain; access/ID tokens never persisted |
| GitHub Copilot (`auth = "github_copilot"`) | `auth/copilot.py`: GitHub.com device flow using OpenCode's OAuth app id (`CLIENT_ID`); the GitHub token is the Copilot API bearer (no `copilot_internal` exchange), verified against `/models` at login | keychain |
| Pasted key (`auth = "keychain"`, e.g. OpenCode Go) | `auth/api_key.py` | keychain |
| Claude subscription (`kind = "claude-agent"`) | `model/providers/claude_agent_auth.py:ClaudeCliAuth`: runs `claude auth login --claudeai` headless (`BROWSER` is a no-op), returns the printed URL, then pipes the code the user pastes (`ProviderLoginCode`) to the CLI | the Claude CLI's own store, shared with Claude Code; Nexus never reads it and never signs it out |

`auth/store.py` is keyring-only (`KeyringSecretStore`); profile names match
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Host flow: `ProvidersStatus`, `ProviderLogin`
+ `ProviderLoginPoll`/`Cancel`, `ProviderKeySet` (the only inward credential
command), `ProviderLogout` in `host_support/provider_auth.py`; a sign-in is a
bounded daemon task returning only a URL and user code. Connecting writes
`[providers.<id>]` to `~/.nexus/config.toml` and, when no turn runs, calls
`Runtime.reload_model_routes`; otherwise restart the daemon. Copilot is
GitHub.com-only; expiring GitHub OAuth tokens require another sign-in. GPT-6
Luna on `/responses` is learned after Copilot rejects `/chat/completions`.
Live `/models` and Luna inference were verified; the device sign-in flow was not
live-tested. CLI: `nexus auth codex login|status|logout`, `nexus claude init`.
Claude sign-in from Settings was verified up to the printed URL and the code
prompt; completing it with a real code was not live-tested (it would replace the
machine's Claude Code login).

### Plan usage and limits (`ProvidersUsage`)

`host_support/provider_usage.py` reads every connected provider concurrently
(40 s cap each, 512 KiB bodies) and returns rows of labelled windows
(`used_percent`, `resets_at` or the provider's `reset_text`, `detail`), notes,
plan and source. Endpoints and field mappings follow CodexBar
(github.com/steipete/CodexBar):

| Provider | Source | Windows |
| --- | --- | --- |
| `codex` | `GET chatgpt.com/backend-api/wham/usage` with the ChatGPT OAuth headers | `rate_limit.primary/secondary_window` (5-hour, weekly by `limit_window_seconds`), `additional_rate_limits[]`, credits and reset-credit notes |
| `claude-agent` | the CLI's `claude -p /usage` (local command, no model request, no tokens read) | `Current session` → 5-hour session, `Current week (…)` → Weekly (…); reset is the CLI's own text |
| `github-copilot` | `GET api.github.com/copilot_internal/user` with the stored GitHub token | monthly `quota_snapshots` (premium requests; chat/completions when metered), reset `quota_reset_date_utc` |
| `opencode-go` | `GET opencode.ai/zen/go/v1/usage` with the stored key | `usage.rolling/weekly/monthly` (`usagePercent`, `resetInSec`) — not verified against a live account |

A provider that fails becomes that row's redacted `error`; the others still
render. Codex, Claude and Copilot were verified live on 2026-10-01.

First run: `SetupStatus` offers packaged candidate models; `SetupSave` writes
`[providers.*]` and `[models].default` to `~/.nexus/config.toml` (blank model
picks the provider's newest tool-calling model). Credentials never enter setup
commands. Claude.ai login is detected without exposing credentials.

### Claude SDK tool handoff

The isolated worker registers the request's Nexus tool schemas on an in-process
`nexus` MCP server using `ClaudeSDKClient`. Native SDK tools remain disabled.
The first assistant tool batch is returned immediately as Nexus tool intentions;
the SDK never executes workspace tools or continues with fabricated results.
MCP handlers only queue intentions if reached during SDK dispatch. Nexus performs
validation, approvals, execution, and durable logging, then supplies actual results
on the next request. Unexpected SDK tool calls fail the provider request rather
than disappearing into final prose. The SDK's `StructuredOutput` formatting tool
is permitted internally. Early handoffs have no final SDK usage report, so usage
for those requests is unavailable (reported as zero). Offline handoff and schema
tests are verified; live subscription inference is not verified.
