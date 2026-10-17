# Adding a provider

How to add a model provider to Nexus and prove it works, from choosing the
smallest route to the live check. [models.md](models.md) describes the provider
layer as it is; [provider-onboarding.md](provider-onboarding.md) is the
stream-inventory checklist you run while doing this. xAI (Grok) is the worked
example at the end.

The rules that shape every provider:

- **One adapter per wire protocol** (plan section 8), not per vendor. A vendor
  that speaks an existing wire is a config block or a thin preset, never a
  second parser.
- **Credentials never leave the daemon** and never enter config: an
  `${env:NAME}` reference resolved at request time, or the private credential
  file through Settings → Providers ([security.md](security.md)).
- **The catalogue describes, it never routes.** models.dev data can name a
  model and its limits; it can never supply a `base_url` or a key.
- **Everything is bounded and redacted.** Keys and custom headers never appear
  in a repr, an error or a log.

## 1. Pick the smallest route

| The vendor… | Route | Code? |
| --- | --- | --- |
| serves a standard OpenAI-compatible API with no quirks | `[providers.<name>]` with `base_url`, `api_key = "${env:…}"`, `api` ([extending.md](extending.md#a-custom-provider)) | none |
| is personal or experimental | a file provider in `.agents/providers/<name>.py` | one file, no core change |
| speaks an existing wire but needs its own endpoint, key or request rules | a **preset**: subclass the wire adapter and override its hooks | small (xAI, below) |
| speaks a new wire protocol | a new adapter module implementing the `Provider` protocol | large |
| is an agent with its own tools (ACP, an SDK) | an agent-surface adapter (`opencode.py`, `claude_agent.py`) | large; read [agents.md](agents.md) first |

Choose the first row that fits. A preset is justified only by behavior a
config block cannot express (a default endpoint and key, a body field the API
requires, a header, an input it rejects). A user can always override the
built-in name with their own `[providers.<name>]` keys.

## 2. Study the wire

Do not design from the model name or the vendor's marketing page. Read a
working client and the vendor's reference docs.

- **Reference code.** Clone a mature client into `reference/` at the repo
  root (the folder holds a `.gitignore` of `*`, so nothing in it is committed):
  `git clone --depth 1 https://github.com/anomalyco/opencode reference/opencode`.
  In OpenCode, look at:
  - `packages/ai/src/providers/<id>.ts`: endpoint, routes, default body
    options, headers.
  - `packages/ai/src/protocols/`: the wire parser and the vendor's extensions
    (`xai-responses.ts`, `openai-chat.ts` compatibility switches).
  - `packages/core/src/variant.ts`: which reasoning controls the vendor takes.
  - `packages/core/src/plugin/provider/<id>.ts`: sign-in methods (API key,
    OAuth device flow).
  - `packages/core/src/session/model-request.ts`: input the vendor rejects.
  - `packages/ai/test/provider/<id>*.test.ts`: event shapes worth copying into
    synthetic fixtures.
- **Catalogue.** `https://models.dev/api.json` → the vendor's entry: `env`
  (the key variable), `npm` (the wire signal the registry maps), models, limits,
  costs, `reasoning_options`.
- **Write down**, before coding: endpoint and dialects, auth header, required
  or default body fields (storage, `include`), reasoning controls and whether
  summaries must be requested, cache-routing headers, media limits, error
  shapes, and whether a token-count endpoint exists. Then run the
  [provider-onboarding](provider-onboarding.md) stream inventory against the
  real endpoint when you have a key.

Keep reference code out of Nexus: re-implement the behavior, cite the
reference file in the module docstring, and keep any copied protocol constants
attributed (see `nexus/auth/NOTICE` for the pattern).

## 3. Implement

Touch only the rows your route needs.

| Concern | Where | What |
| --- | --- | --- |
| Adapter name | `nexus/model/registry.py` | an `ADAPTER_<ID>` constant; add it to `_KNOWN_ADAPTERS`, the catalogue's `npm` in `_NPM_ADAPTERS`, the provider id in `_DIRECT_ADAPTERS` |
| Adapter | `nexus/model/providers/<id>.py` | a preset subclass, or a full adapter: `name`, `capabilities(model)`, `stream(req)`, `count_tokens(req)`, `aclose()` |
| Construction | `nexus/runtime.py` | the name in `_CORE_ADAPTERS`; a branch in `_construct_provider` that applies the section's auth (and refuses auth modes that belong to other vendors) |
| Config rules | `nexus/config/schema.py` | only if the section's validation changes (for example which `kind` may use `auth = "keychain"`) |
| Offline catalogue | `nexus/model/data/models.min.json` | a few current tool-calling models copied from models.dev, in the file's existing style; never invent prices or limits |
| Sign-in | `nexus/auth/<id>.py`, `nexus/host_support/provider_auth.py` | an OAuth manager (`status`, `device_login`/`browser_login`, `headers`, `logout`) when the vendor has a subscription sign-in; a `PROVIDERS` row (label, methods, help), `_manager`, the login branch in `provider_login`, `_route` (the `[providers.<id>]` keys a connection writes), `_LOGOUT` |
| Auth mode | `nexus/config/schema.py`, `nexus/runtime.py` | a new `auth` value pinned to its section name and endpoint, and a `<id>_auth_factory` seam for tests |
| First run | `nexus/host_support/setup.py` | `_PROVIDERS`, `_ENV_NAMES`, `_PROVIDER_INFO`, `_connected`, `_provider_config` |
| Plan usage | `nexus/host_support/provider_usage.py` | only if the vendor publishes a usage endpoint |
| Tiers | `nexus/model/tiers.py` | only when asked; built-in tier routes are a product decision |

Adapter rules:

- **Presets override hooks, not parsers.** `OpenAIProvider` exposes
  `_prepare_request(req)` (rewrite the IR request before translation; never the
  durable history), `_finalize_body(body, api)` (vendor body fields),
  `_headers(body, session_id)` (vendor headers), and `supports_count_tokens()`.
  Add a hook to the base adapter rather than copying a method.
- **Normalize once.** Every wire event maps to `nexus/model/stream.py`
  (`MessageStart`, `TextDelta`, `ThinkingDelta`/`ThinkingEnd`, tool-call
  events, `Usage`, `MessageStop`) as tabled in
  [provider-onboarding.md](provider-onboarding.md#normalize-semantics-once).
  New user-visible semantics extend the shared contract, the event log, the
  reducer and both clients together.
- **Declare degradation.** `Capabilities.degradation` says what happens to
  thinking, images and documents the model cannot take (`drop`, `to_text`,
  `error`). The registry is authoritative for model capabilities; the adapter
  table is the fallback.
- **No token-count guessing.** Return `None` from `count_tokens` unless the
  vendor publishes an endpoint; the context layer falls back to the heuristic.
- **Errors.** Raise the shared provider errors (`ProviderError`,
  `ProviderOverloaded`, …) so the loop's retry and fallback rules apply; the
  shared transport already redacts.

Clients need no change for a provider that signs in with an API key: the
native and desktop Settings pages render `ProvidersStatus` rows and send
`ProviderKeySet`. The web client keeps a hard-coded list and is deprecated;
do not port to it unless asked.

## 4. Test

All offline tests use `httpx.MockTransport`; no network, no real key.

| Test | File | Proves |
| --- | --- | --- |
| Adapter unit tests | `tests/test_<id>_provider.py` | URL, auth header, body fields, vendor headers, input rewrites (and that the durable request is untouched), stream normalization from a synthetic SSE fixture, usage and stop mapping, missing-key error, repr redaction, `count_tokens` without a network call |
| Conformance matrix | `tests/provider_conformance/adapters.py` (adapter class, tags, `AdapterSpec`), `tests/test_provider_conformance.py` (`_secret_adapter`) | the shared case catalogue (streaming, tools, thinking, retries, cancellation, close, secrets) passes on the new adapter; tags drop what it does not support |
| Thinking lifecycle | `tests/test_thinking_context.py` | only when the provider adds new thought semantics |
| Routing | `tests/test_<id>_provider.py` | `Runtime` built from a `nexus.toml` section picks the adapter, reads the env key and a pasted key, and refuses the wrong auth modes |
| Catalogue | same file | `map_provider` maps the id and `npm`; the snapshot parses and lists the models |
| Sign-in flow | same file | for OAuth: the device or browser flow against a mock issuer (pending, `slow_down`, denied, expired, cancelled, a foreign verification URL), what is stored (and what is not), refresh before expiry, rotation, a rejected refresh, logout |
| Settings and setup | same file, plus the list assertions in `tests/test_host_provider_auth.py`, `tests/test_host_setup.py` and `tests/test_ratatui_journeys.py` | the Settings row and methods; a completed sign-in or pasted key writes the exact route and never echoes a credential; logout removes it; setup offers the provider only when it is really connected |
| Live (gated) | `tests/test_<id>_live.py` | `pytestmark = [pytest.mark.live, skipif(…)]` gated on a key or an explicit flag plus an existing sign-in; one bounded request through the adapter and one full turn through the loop; asserts real text, usage and a normal stop |

Run:

```sh
.venv/bin/python -m pytest -q tests/test_<id>_provider.py tests/test_provider_conformance.py
.venv/bin/python -m pytest -q tests/test_host_provider_auth.py tests/test_host_setup.py tests/test_docs.py
.venv/bin/python -m pytest -q          # full offline suite
ruff check nexus tests
<KEY>=… .venv/bin/python -m pytest -q -m live tests/test_<id>_live.py   # with a real key
```

Then check the product by hand with a key: paste it in Settings → Providers
(or export the variable and restart the daemon with `nexus daemon restart`),
pick the model with `/model`, and run one turn in `nexus chat` and one with
`nexus run "Reply with one word"`. Watch for public thinking, tool calls, usage
in the context meter, and a clean stop.

## 5. Document

In the same change:

- [models.md](models.md): the adapter row in **Providers** and the sign-in row
  in **Authentication**.
- [provider-onboarding.md](provider-onboarding.md): a **Baseline
  observations** row saying what was observed and how (live or offline only).
- [module-map.md](module-map.md): a row for the new module (`tests/test_docs.py`
  fails without it).
- [decisions.md](decisions.md): why this route (config, preset or adapter).
- [config.md](config.md): only if config keys or validation changed.

Say "not verified" for anything not exercised against the real endpoint.

## Worked example: xAI (Grok)

**What the reference showed** (OpenCode, `packages/ai/src/providers/xai.ts`,
`protocols/xai-responses.ts`, `core/src/plugin/provider/xai.ts`,
`core/src/session/model-request.ts`):

| Finding | Nexus decision |
| --- | --- |
| Endpoint `https://cli-chat-proxy.grok.com/v1` (Grok CLI subscription proxy), the reference's route. Verified on the API host `api.x.ai` only: Responses (live turns, 2026-10-14) and Chat Completions (route exists). Not verified on the proxy: authenticated inference on either route (only an unauthenticated `OPTIONS` and an invalid-bearer `POST` were checked), so proxy compatibility is unverified live | preset over `OpenAIProvider`, `api = "responses"` by default, `api = "chat"` selectable |
| Grok streams the standard `response.reasoning_summary_text.*` events | no parser change; the shared Responses decoder handles it |
| OpenCode sends `store: false` (xAI otherwise keeps responses server-side) and `include: ["reasoning.encrypted_content"]` | `store: false` on every Responses request; encrypted reasoning is not requested because the OpenAI adapter does not replay it |
| `x-grok-conv-id` routes a conversation to one prompt cache (Chat Completions route) | the Nexus session id is sent as `x-grok-conv-id`; its effect on `/responses` is not verified |
| xAI rejects images other than PNG, JPEG and WebP, and a stored bad image fails every later turn | `_prepare_request` turns other image blocks into a text note on the wire only |
| Reasoning effort is a Responses effort (`low` … `xhigh`, per model in models.dev) | the shared effort path; efforts come from the catalogue |
| Sign-in: API key, or a SuperGrok device-code OAuth (`auth.x.ai/oauth2`, OpenCode's client id, scope `… grok-cli:access api:access`); the access token is used exactly like an API key | **SuperGrok only** (`auth = "xai_oauth"`, `nexus/auth/xai.py`): device code in Settings → Providers, refresh token stored, access token refreshed 5 min before expiry; every API-key route is refused |
| No input-token counter | `supports_count_tokens()` is `False` |
| Hosted tools (`x_search`, `web_search`), WebSocket transport | not implemented |

**Files:** `nexus/model/providers/xai.py` (preset) and `nexus/auth/xai.py`
(sign-in, attributed in `nexus/auth/NOTICE`); `ADAPTER_XAI` and the
`@ai-sdk/xai` mapping in `nexus/model/registry.py`; `_CORE_ADAPTERS`,
`xai_auth_factory` and `_construct_xai` in `nexus/runtime.py`; the `xai_oauth`
auth mode in `nexus/config/schema.py`; the `xai` rows in `provider_auth.py`
(a `device` method) and `setup.py`; three Grok models in `models.min.json`.

**Tests:** `tests/test_xai_provider.py` (preset, device flow against a mock
`auth.x.ai`, refresh and rotation, subscription-only routing, Settings and
setup), the `xai_responses` conformance spec, and the gated
`tests/test_xai_live.py`.

**Configuration:** connect xAI (SuperGrok) in Settings → Providers, which
writes:

```toml
[providers.xai]
auth = "xai_oauth"
api = "responses"
```

Then `[models] default = "xai/grok-4.7"`, or `/model xai/grok-4.7`.

**Verification (2026-10-14):** live sign-in from Settings (xAI accepted
`referrer = nexus`), a `nexus chat` turn on grok-4.7, and both tests in
`NEXUS_XAI_LIVE=1 .venv/bin/python -m pytest -q -m live tests/test_xai_live.py`.
Not yet observed live: a token refresh after expiry, reasoning summaries, and
whether `x-grok-conv-id` (rather than xAI's automatic caching) produced the
cache hits on `/responses`.

The live test first skipped even though the sign-in worked: `tests/conftest.py`
gives every test a private `NEXUS_HOME`, so a manager built inside a test reads
an empty credential file. A live test must resolve the real credential path at
import time and inject it (`FileSecretStore(path=…)`, `xai_auth_factory=`).
