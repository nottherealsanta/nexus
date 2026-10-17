# Models, providers and sign-in

`nexus/model/` (L1) is provider-neutral: message IR, the `Provider` protocol,
adapters, the router, the models.dev registry and tiers. `nexus/auth/` holds
file-backed sign-in. To add a provider, follow
[adding-a-provider.md](adding-a-provider.md); to add or change a wire dialect,
run the [provider-onboarding.md](provider-onboarding.md) stream inventory as well.

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
| `openai.py` | `openai`, `codex`, any `kind = "openai_compatible"` | Responses and Chat Completions behind one class (`api = "responses"\|"chat"`); `EndpointFallback` remembers the dialect a model accepts when a host serves some models on only one endpoint (GitHub Copilot, OpenCode Go) |
| `gemini.py` | `google`, `gemini`, `kind = "google"` | `generateContent` streaming; synthetic call ids |
| `xai.py` | `xai` | xAI Grok preset over `OpenAIProvider`: the Grok CLI subscription proxy `https://cli-chat-proxy.grok.com/v1` (sends `x-xai-token-auth: xai-grok-cli`; authenticated inference there is not yet verified live), `api = "responses"` default (`chat` selectable). **Subscription only:** the section must set `auth = "xai_oauth"` (SuperGrok sign-in); an API key, an env variable, a bare section or any other `kind = "xai"` route is a `ConfigError`. `store: false` on Responses; session id as `x-grok-conv-id`; non-PNG/JPEG/WebP images become a text note; no token counter. Verified live 2026-10-14: SuperGrok device sign-in from Settings (xAI accepted `referrer = nexus`) and grok-4.7 turns over `/responses` with usage and a normal stop (`tests/test_xai_live.py`, `nexus chat`). This is historical verification against `api.x.ai`, before the switch to the Grok CLI proxy; it does not verify current inference through the proxy |
| `ollama.py` | `ollama` | native `/api/chat` or OpenAI-compatible mode; keyless, loopback default `http://localhost:11434`; conservative capabilities (`tools = false` unless the registry says otherwise) |
| `opencode.py` | `opencode` (`kind` `acp`) | OpenCode over its Agent Client Protocol subprocess only; ACP tool calls stay inside that agent; child env is an explicit allowlist |
| `claude_agent.py` (+ `_claude_agent_worker.py`, `claude_agent_auth.py`) | `claude-agent` | included in every install; text-only, buffers replies; SDK built-ins, hooks, settings and persistence disabled; Nexus schemas registered through an in-process MCP intention bridge |
| `claude_code` kind (no adapter file) | `claude-code` | Main-agent selection reuses `ClaudeAgentProvider` (as `claude-agent`) and requires `[models] claude_code_main_enabled = true`. Worker backend (`claude_code_subagent_backend = "worker"`, default) is subagent-only and hidden from main model pickers; use `"sdk"` for ordinary SDK-backed main routes. Worker-based subagent pickers expose exact Claude subscription catalogue rows even when main selection is disabled. An explicit worker subagent is run by `agents/claude_code_worker.py` with Claude's own tools, never through a provider `stream()` ([agents.md](agents.md#external-claude-code-workers)). Registry projection reuses the Claude subscription catalogue with text-only capabilities; model IDs are not verified live |
| `antigravity` kind (no streaming adapter) | `antigravity` (alias `agy`) | **Subagent-only** external CLI. Main-model pickers exclude it; worker/task/role/tier subagent routes can select it. It is never a main, plan, fast, fallback or title model (`ConfigError` if named there). Models are the exact IDs `agy models` reports (probed locally by `model/providers/antigravity_cli.py`, never hard-coded; an unreported ID is refused with a refresh hint). Run by `agents/antigravity_worker.py` with agy's own tools, never through a provider `stream()` ([agents.md](agents.md#external-antigravity-workers)). Implemented offline only: quota usage is read from the built-in `agy -p /usage` TSV format verified with agy 1.3.4; no live model turn was tested and no credentials were used |
| `scripted.py` | tests | deterministic offline provider ([testing.md](testing.md)) |
| `discovery.py` | `.agents/providers/*.py`, `~/.nexus/providers/*.py` | quarantined file providers (`PROVIDER`, `PROVIDERS` or `build(context)`); a configured section always wins a same-named file; legacy `.nexus/providers/` is a lower-precedence fallback |
| `devtools/mock/provider.py` | dev mode | `mock/<scenario>` models ([devtools.md](devtools.md)) |

`ProviderSection` keys: `kind`, `api_key`, `base_url`, `api`, `auth`
(`chatgpt_oauth` \| `github_copilot` \| `keychain` \| `xai_oauth`), `profile`, `executable`,
`timeout_seconds`, `command`, `args`, `permission_policy`, `inherit_env`, `env`,
and, for `kind = "claude-code"` only, the `worker_*` keys
([config.md](config.md#claude-code-worker-keys)), with the Antigravity exceptions
below. The shared `worker_inherits_agy_settings` default is ignored by other kinds.
`kind = "antigravity"` (alias `agy`) accepts only `executable`, `timeout_seconds`,
`worker_inherits_agy_settings` and `worker_permission_mode`
([config.md](config.md#antigravity-worker-keys)); any other field is refused.
A `base_url` must be http(s); plain `http` only for loopback; any `user:pass@`
userinfo is always redacted. `auth = "xai_oauth"` is accepted only on
`[providers.xai]`, with no `api_key` and no `base_url` other than
`https://cli-chat-proxy.grok.com/v1`, so the subscription token can only reach `cli-chat-proxy.grok.com`. An OpenAI-compatible vendor **must** have a
`base_url`, otherwise its model id and key could go to `api.openai.com`
(a hard `ConfigError`).

## Routing (`router.py`)

Resolution order, first hit wins:

1. config aliases (`default`, `fast`, `plan`, explicit aliases);
2. a **tier name** (`low`/`medium`/`high` or a custom `[models.tiers]` name): the
   first selectable model whose provider the router can stream;
3. a registry reference: `provider/model`, a bare id, or an aggregator alias;
4. adapter fallback: split `provider/model` and use the adapter's own capabilities.

Tier names select subagent routes, not main models or titles. The router also
accepts legacy tier-valued main config for compatibility; new main selections
are concrete IDs. Unpinned built-in subagent tiers prefer
these ordered routes (skipping providers that are not configured):

| Tier | Primary | Ordered fallbacks |
| --- | --- | --- |
| Low | Copilot GPT-6 Luna | Codex GPT-6 Luna → OpenCode Go DeepSeek V4.1 Flash |
| Medium | Codex GPT-6.1 Sol, low effort | OpenCode Go DeepSeek V4.1 Flash, max effort |
| High | Codex GPT-6.1 Sol, low effort | Claude Agent Sonnet 5.5 |

References use `codex/gpt-6.1-sol` and `claude-agent/claude-sonnet-5-5`.
The new Sol and Sonnet catalogue entries are provisional: provider availability,
pricing, and limits are **not verified** (no prices or limits are fabricated); their baseline capabilities follow the
existing bundled model families. Explicit tier pins replace built-in preferences
and fallback chains. Explicit session/agent effort choices (including Default)
win over route effort defaults; fallback requests apply their own route effort.
Root route/effort metadata and context assembly expose these defaults.

`ModelRouter.fallbacks` only *lists* the agent, tier, then global
`model.fallback` chains; the loop decides when to try one
([loop.md](loop.md#failure-handling)). Per-session `/model` and effort choices
are durable (`model.selected`, `reasoning_effort.selected`).
Explicit effort choices (including Default) are also remembered per concrete
provider/model in bounded, best-effort machine state at
`~/.nexus/model_efforts.json`. Selecting that model again restores its effort
through normal durable session events, across sessions and daemon restarts.
Unsupported remembered efforts fall back to Default; models without a remembered
choice retain the existing selection behavior. Selectable host model rows expose
`remembered_effort` only when a preference exists. Native model selection skips
the effort prompt for remembered models; `/effort` remains available to change it.

## Registry and tiers

- `ModelRegistry` is data and lookup only; it does no I/O during a turn.
  Acquisition is fetch-on-first-use with a TTL (`models.refresh_ttl_days`, 7);
  on failure it falls back to a valid stale cache, then the vendored snapshot,
  then empty. The snapshot lists Nexus's signed-in providers (OpenCode Go,
  GitHub Copilot) as well as the major direct APIs, so a connected provider
  still appears on the model list when the catalogue cannot be fetched.
  `models.offline = true` never fetches. `ModelsRefresh` forces one.
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
  → no cost data reads as `low`. Role/nested-child tier clamps only ever
  narrow.
- **A tier runs the first runnable model of the user's list.**
  Read-only is prompt guidance only (user decision 2026-10-17), so no child
  (including `advisor`) skips an Antigravity route for being read-only.
  `[models.tiers]` lists are ordered; `ModelRouter._first_pinned` walks the list in order and uses
  the first model whose provider can stream, before falling back to the
  catalogue's order. Settings → Models edits these lists (`ModelTierSet`,
  `ModelTierReset`, global `config.toml`) and rebuilds routes while no turn runs.
  `ModelRouter.tier_runnable(tier)` says whether a tier resolves; it is `False`
  without a registry, because a plain config has no tiers and a bare `low` would
  be sent to the provider as a model id.
- Tiers are **subagent-only**; main-model selection and automatic titles use
  concrete provider/model IDs. Settings stacks every tier on one page (no tabs
  or highest-tier control). Each entry has supported effort choices, saved in
  `[models.tier_efforts.<tier>]` as `"provider/model" = "effort"`. Default removes
  the override. Routing applies each fallback model's own effort; an explicit
  role effort wins. The subagent tool lists each tier's ordered provider/model
  IDs and tells the caller to try the next listed model if a child route fails.
- Antigravity discovery probes the built-in `antigravity` route even when a
  differently named Antigravity section exists. Reported models appear in the
  subagent picker, never the main/title picker. Execution inherits agy settings by default; an explicit opt-out and all
  permission/restriction gates still apply.

## Authentication

| Route | Code | Storage |
| --- | --- | --- |
| API key (`${env:NAME}` reference) | adapter at request time | never stored by Nexus |
| ChatGPT / Codex OAuth (`auth = "chatgpt_oauth"`) | `auth/codex.py`: browser PKCE or device code; protocol pinned to a cited OpenCode commit (see `auth/NOTICE`) | rotating refresh token + routing metadata in the private credential file; access/ID tokens never persisted |
| GitHub Copilot (`auth = "github_copilot"`) | `auth/copilot.py`: GitHub.com device flow using OpenCode's OAuth app id (`CLIENT_ID`); the GitHub token is the Copilot API bearer (no `copilot_internal` exchange), verified against `/models` at login | private credential file |
| Pasted key (`auth = "keychain"`, e.g. OpenCode Go) | `auth/api_key.py` | private credential file |
| xAI SuperGrok (`auth = "xai_oauth"`) | `auth/xai.py`: device code at `auth.x.ai/oauth2` with OpenCode's xAI OAuth client id and scope (see `auth/NOTICE`); the access token is the Bearer for `cli-chat-proxy.grok.com`, refreshed 5 min before expiry under the cross-process credential lock | refresh token only (rotations saved) in the private credential file; access token in memory. Sign-in verified live 2026-10-14 with `referrer = nexus`; a refresh after expiry has not yet been observed live. Inference requests also send `x-grok-client-version` (from `NEXUS_XAI_CLIENT_VERSION` when set, otherwise `~/.grok/version.json`; the proxy answers HTTP 426 without it), `x-grok-client-identifier: grok-shell` and user agent exactly `xai-grok-cli` (the string the CLI proxy requires; Nexus is not the official Grok CLI) (`model/providers/xai.py`). Inference with these headers is **not verified live** (no authenticated request was made) |

OpenCode Go (`opencode.ai` base URLs) additionally gets an `x-opencode-session` header on every chat/responses request, set to the Nexus session id (the loop stamps it into `ModelRequest.metadata["session_id"]`); the gateway asks for one stable id per conversation.
| Claude subscription (`kind = "claude-agent"`) | `model/providers/claude_agent_auth.py:ClaudeCliAuth`: runs `claude auth login --claudeai` headless (`BROWSER` is a no-op), returns the printed URL, then pipes the code the user pastes (`ProviderLoginCode`) to the CLI | the Claude CLI's own store, shared with Claude Code; Nexus never reads it and never signs it out |

OpenCode Go (`opencode.ai` base URLs) additionally gets an `x-opencode-session` header on every chat/responses request, set to the Nexus session id (the loop stamps it into `ModelRequest.metadata["session_id"]`); the gateway asks for one stable id per conversation.

OpenCode Go serves different models on different protocols — for example GPT-6 Luna and Grok answer only on `/responses`, while Kimi and GLM answer only on `/chat/completions` — and rejects the other with `Model does not support this protocol.` The route defaults to `chat` and carries an `EndpointFallback` (the same mechanism Copilot uses) that retries the other endpoint once and remembers the model's dialect.

`auth/store.py` is file-backed (`FileSecretStore`); profile names match
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
xAI signs in from Settings → Providers only (no CLI subcommand).
Claude sign-in from Settings was verified up to the printed URL and the code
prompt; completing it with a real code was not live-tested (it would replace the
machine's Claude Code login).
Antigravity has no Nexus sign-in at all: `agy` has no `auth` subcommand, so
Settings → Providers shows a manual `cli` method ("run `agy` in a terminal and
sign in there", cached login) and never starts, reads or signs out a login.
`AntigravityCli` can only prove the CLI is present (`cli_present`, version);
`auth_verified` is always `False`. Install state and sign-in are separate facts.

### Plan usage and limits (`ProvidersUsage`)

`host_support/provider_usage.py` reads every connected provider concurrently
(40 s cap each, 512 KiB bodies) and returns rows of labelled windows
(`used_percent`, `resets_at` and/or the provider's `reset_text`, `detail`,
`period_seconds`), notes, plan and source. `period_seconds` is the window's
length: Codex's `limit_window_seconds`; 5 hours or a week for OpenCode Go and
Claude windows; for a monthly window, the calendar month (UTC) ending at its
reset. Claude's reset text (`Oct 15 at 8:30pm (Asia/Calcutta)`, or a bare
`12pm (…)` meaning the next one) is parsed in its named zone into `resets_at`;
the year is inferred (the earliest match no more than a day in the past), and
text that does not parse leaves `resets_at` empty. Endpoints and field mappings
follow CodexBar (github.com/steipete/CodexBar):

| Provider | Source | Windows |
| --- | --- | --- |
| `codex` | `GET chatgpt.com/backend-api/wham/usage` with the ChatGPT OAuth headers | `rate_limit.primary/secondary_window` (5-hour, weekly by `limit_window_seconds`), `additional_rate_limits[]`, credits and reset-credit notes |
| `claude-agent` | the CLI's `claude -p /usage` (local command, no model request, no tokens read) | `Current session` → 5-hour session, `Current week (…)` → Weekly (…); reset is the CLI's own text |
| `github-copilot` | `GET api.github.com/copilot_internal/user` with the stored GitHub token | monthly `quota_snapshots` (premium requests; chat/completions when metered), reset `quota_reset_date_utc` |
| `opencode-go` | `GET opencode.ai/zen/go/v1/usage` with the stored key | `usage.rolling/weekly/monthly` (`usagePercent`, `resetInSec`) — not verified against a live account |
| `xai` | `GET cli-chat-proxy.grok.com/v1/billing?format=credits` with the SuperGrok bearer and `x-xai-token-auth: xai-grok-cli`; plan from `/v1/settings` `subscription_tier_display` (3 s, best effort) | one window from `config.currentPeriod` (else `billingPeriodStart/End`), labelled Weekly/Monthly by its length; `creditUsagePercent`, else `onDemandUsed/onDemandCap`; an omitted percent with a current period reads 0% (the JSON drops zeros) and says so; `productUsage` shares and `prepaidBalance` (cents) as notes. Verified live 2026-10-14 (SuperGrok Lite, weekly) |

`antigravity` is honestly unavailable: its usage row (shown once an explicit `[providers.antigravity]`
section exists; the Providers Settings row is always present) has empty `windows`, source `agy CLI`, and a note to run `agy` and
enter `/usage` manually. Nexus does not run `agy -p /usage` yet because no output
format is verified, and it shows no quota numbers. Eventual run usage comes only
from agy's terminal `result.usage`; tokens are reported when present and cost is
always `unknown`.

A provider that fails becomes that row's redacted `error`; the others still
render. Codex, Claude and Copilot were verified live on 2026-10-01; xAI on
2026-10-14 (CodexBar's `docs/grok.md` documents the endpoint; the SuperGrok
token from Nexus's own sign-in is accepted).

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

Credentials are plaintext in `~/.nexus/credentials.json` (`0600`, directory
`0700`), or `$NEXUS_HOME/credentials.json`. No keychain fallback or automatic
migration is performed: sign in again after upgrading. The legacy
`auth = "keychain"` spelling still selects pasted API keys.


## Default tier routing

Settings → Models shows each tier's list in this run order (`ModelRouter.tier_candidates`, shared with resolution so the label and the page cannot disagree) and edits the default model chain (`models.default` plus `models.fallback`, global config) through `DefaultModelSet`.

The tier **Add model** picker lists routes selectable by subagents (including
external worker routes such as Antigravity and Claude); the default-chain
picker remains limited to main-agent routes. Adding a tier route changes that
tier's ordered preferences and does not make it available to the main agent.

Subagent tier requests use these ordered model preferences. An explicitly configured `[models.tiers]` list takes precedence. Provider or model failures can move to the next model before ordinary workspace/agent fallback handling; agent fallbacks retain priority over the global `model.fallback` chain.

| Tier | Ordered route | Default reasoning effort |
| --- | --- | --- |
| `low` | `github-copilot/gpt-6-luna` → `openai-codex/gpt-6-luna` → `opencode-go/deepseek-v4.1-flash` | provider default |
| `medium` | `openai-codex/gpt-6.1-sol` → `opencode-go/deepseek-v4.1-flash` | `low` on Codex; `max` on DeepSeek |
| `high` | `openai-codex/gpt-6.1-sol` → `claude-agent/claude-sonnet-5.5` | `low` on Codex; provider default on Claude |

A user-selected effort remains authoritative, and the tier effort applies consistently to fallback attempts when that fallback has a tier-specific default.

### Probe cache and tier reload

The `agy models` result is cached, not re-run per registry build. `Runtime`
owns a `RegistryCache` handed to every `ModelRegistry` it builds, so a route
reload (`reload_model_routes`: provider or default-model change) reuses the
list and the already-parsed models.dev catalogue (keyed on path, mtime, size).
The list is re-probed only by the provider's own **Refresh models** action
(`ProviderModelsRefresh`, Settings > Providers > Antigravity; the models.dev
`ModelsRefresh` never re-runs `agy`), when the resolved `agy` path or the
section changes, or when nothing is cached. A failed probe keeps the last good
list and records the error. Good lists persist, with their refresh time, as `~/.nexus/cache/agy-models.json`
beside `models.dev.json` (bounded, atomic; corrupt means probe), so a daemon
restart does not wait on `agy`. Tier-only edits (`ModelTierSet`, `ModelTierReset`)
call `Runtime.reload_tier_config`: new `TierTable`, registry re-index from the
held catalogue (`ModelRegistry.retier`), `ModelRouter.set_tiers`, and the
context coordinator; providers and the registry object are untouched.
The host always requests this reload, **even while a turn runs** (it touches no
provider, so the idle guard of `reload_model_routes` is not needed) and returns
`restart_required=False`; it is `True` only when the runtime was built with injected
routes and cannot reload. A subagent's model is resolved when the spawn tool call
creates its session (`ModelRouter.resolve` -> `tier_candidates`), so reordering a tier
makes the next spawn and the next turn use the new first model. Running children and
the in-flight request keep the model they already chose. Not covered: a runner built
for an already-running turn keeps its construction-time config for the roster's effort
labels and the tier-name lookup of concrete refs until the next turn.

### Antigravity visibility and safe discovery

An installed `agy` is discovered without a handwritten provider section using a
bounded `agy models` probe only. Configured `agy` aliases and custom executables
are respected. The host model list exposes these rows labelled **Subagents only**
(including native clients); selecting one as the main model is refused with an
explanation, while explicit subagent tier/role/Task references remain supported.
Worker execution inherits agy's own user/global settings, MCP servers and plugins
by default, even without a provider section; Nexus PathGuard does not apply inside
agy. Set `worker_inherits_agy_settings = false` to refuse execution. Tool
restrictions remain refused; read-only roles are not (guidance only).

Providers Settings always includes Antigravity, with manual CLI sign-in: run `agy`
in your terminal, then **Refresh models** (re-runs `agy models`; the page shows the count and last-refreshed time). CLI presence is not verified
authentication. Nexus does not run a login subcommand, manage CLI credentials, or
perform a model turn to discover availability. A missing CLI is reported honestly.

### Antigravity discovery output

The local `agy models` parser accepts both bare model IDs and the current
`model-id<TAB>display name` format. Display labels are sanitized and bounded;
IDs remain exact and validated. Verified against the installed CLI's local
model listing (no model turn or credential operation). This fixes empty
subagent pickers when a CLI returns labelled TSV rather than bare IDs.
