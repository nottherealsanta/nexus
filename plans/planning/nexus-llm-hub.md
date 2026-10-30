# 14 — Provider core: one declarative OpenAI family, typed errors, visible degradation

Status: **planning**. Nothing here is implemented or verified.
Inspiration: [smhanov/llmhub](https://github.com/smhanov/llmhub) (Go, MIT). We borrow its ideas, not its code.

This plan replaces an earlier draft that ported the HTTP providers to a Rust
crate behind PyO3. That draft is kept as §11 (deferred). The structural wins it
named (declarative presets, typed errors, timing and degradation events, cheap
OpenAI-compatible vendors) do not depend on the language. Doing them in Python
avoids a first PyO3 extension, a second async runtime, and two engines kept in
sync. Python's httpx path is not a measured bottleneck.

## 1. Goal

Improve the HTTP providers in place, in `nexus/model/`, behind the existing
`Provider` protocol (`nexus/model/provider.py`):

1. **Quirks as data.** Every OpenAI-family host is a row in one preset table, not
   a branch in `openai.py` or a subclass hook.
2. **Typed provider errors.** Adapters raise classified errors, and the loop stops
   substring-matching messages.
3. **Nothing dropped silently.** Every parameter an adapter drops or rewrites is
   reported as a `Degraded` event and shown in both UIs.
4. **Timing.** Connect, first byte, first token and total time per request, as a
   `Timing` event.
5. **Cheap vendors.** Once (1) exists, an OpenAI-compatible vendor costs a preset
   row, a registry mapping and a fixture.

In scope: Anthropic, OpenAI (Responses and Chat), Gemini, Ollama, and the
OpenAI-family hosts Codex, GitHub Copilot, xAI, NVIDIA and OpenCode Go.

Out of scope:

- Subprocess and agent providers: `opencode` ACP, `claude-agent`, the
  `claude-code` worker, `antigravity`/`agy`. Also `scripted` and user file
  providers. They gain the typed errors only if they choose to raise them.
- Routing, the models.dev registry, tiers, the usage gate, sign-in flows and
  credential storage. Nothing changes there except new rows in `_NPM_ADAPTERS`.

## 2. What we take from llmhub

| llmhub idea | Adopt? | Notes |
| --- | --- | --- |
| Ordered content parts per message | Already have | `message.py`. |
| Reasoning as a first-class part | Already have | Including `ThinkingEnd(signature)`, which llmhub drops. |
| One shared OpenAI-compatible core configured by a **quirks struct** | Yes, the main structural win | §4. |
| One thinking-level enum, with documented per-provider clamping | Yes | One table per adapter, tested (§4.5). |
| Timing metrics (connect, first byte, first token) | Yes, new | `Timing` event (§6). |
| Retry honours `Retry-After`, adds jitter | Already have | `nexus/model/http.py` retries 408/409/425/429/5xx, never after the first event. |
| HTTP errors surface before the stream starts | Already have | The loop's failover contract. |
| `extra_body` escape hatch merged last | Yes, bounded | Per provider section in config only. Never set by the catalogue. Every key it sets is listed in the request's `Degraded`/info row. |
| Untyped string errors | **No** | Typed errors (§5). |
| Unsupported options silently ignored | **No** | `Degraded` event (§6). This is the Nexus "show everything" rule. |
| Gemini empty tool ids, key in query string, 1024 `max_tokens` default | **No** | Nexus already does better. |

## 3. Current state (surveyed 2026-10-27)

- `nexus/model/providers/openai.py` (1586 lines) holds the Responses and Chat
  dialects plus host rules as branches:
  - `_is_codex_host`: drops `temperature`/`top_p` (`_finalize_body`), sets
    `prompt_cache_key` and a `session_id` header.
  - `_is_opencode_host`: sets `x-opencode-session`.
  - `_is_openai_host`: gates `count_tokens` via `/v1/responses/input_tokens`.
  - Reasoning effort is silently cleared on Responses when the model has no
    thinking capability.
- `xai.py` and `nvidia.py` subclass `OpenAIProvider` and override
  `_prepare_request`, `_finalize_body` and `_headers` (xAI: `store: false`,
  `x-grok-*` headers, image handling).
- Copilot is the OpenAI adapter plus `CopilotHeaders`, whose `request_headers(body)`
  adds per-request headers (`runtime.py` ~L6120–6180).
- `EndpointFallback` learns a per-model dialect in memory.
- `nexus/errors.py` has `ProviderError`, `ProviderOverloaded` and
  `MalformedToolCall`; `nexus/model/capabilities.py` has `CapabilityRejected`.
  `http.py` sets `status_code` on the error. `core/loop.py:_rejected_feature`
  falls back to the `_CAPABILITY_FEATURE_HINTS` substring table.

## 4. OpenAI family: quirks as data

### 4.1 The table

New module `nexus/model/providers/openai_presets.py`:

```python
@dataclass(frozen=True, slots=True)
class Quirks:
    dialects: tuple[str, ...] = (API_RESPONSES,)    # first is default; >1 enables EndpointFallback
    drop_params: frozenset[str] = frozenset()       # Codex: {"temperature", "top_p"}
    cache_key: CacheKeyMode = CacheKeyMode.NONE     # BODY_PROMPT_CACHE_KEY | HEADER
    session_header: str | None = None               # "session_id", "x-opencode-session", "x-grok-conv-id"
    session_id_max: int = 64
    static_headers: tuple[tuple[str, str], ...] = ()  # x-grok-client-*, originator
    body_overrides: tuple[tuple[str, object], ...] = ()  # xAI: (("store", False),)
    reasoning_field: ReasoningField = ReasoningField.SUMMARY  # | REASONING_CONTENT | REASONING
    tool_result_images: ImageMode = ImageMode.INLINE  # | TO_TEXT | DROP
    count_tokens: bool = False                      # /responses/input_tokens
    verified_live: bool = False                     # shown in docs and Settings

@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    base_url: str
    quirks: Quirks
    env_key: str | None = None                      # default ${env:…} key

PRESETS: Mapping[str, Preset] = MappingProxyType({...})
```

Values only, with no callables. A preset that seems to need code goes in §4.3
instead.

### 4.2 Selecting a preset

- A provider section may name it: `preset = "codex"` (new config key, documented
  in `docs/config.md`). Built-in provider names (`codex`, `xai`, `nvidia`,
  `github-copilot`, OpenCode Go) map to their preset by default.
- If no preset is named, today's host detection picks one, so existing configs
  keep working. A plain OpenAI-compatible endpoint gets the empty `Quirks()`.
- The active preset is shown in `nexus doctor` and the Settings provider row.

### 4.3 What stays code

- Auth: OAuth and Copilot header factories stay as they are (`auth_headers`,
  `request_headers(body)`). A preset never carries credentials.
- Anything that rewrites messages rather than parameters, such as xAI image
  handling, stays a small named function referenced by the preset
  (`message_filter: str | None`, resolved from a fixed registry in the same
  module). It does not become a subclass.
- After the refactor `XAIProvider` and `NvidiaProvider` are removed, or reduced to
  aliases if config or tests import them by name. That is decided during
  phase 1, when the imports are checked.

### 4.4 `OpenAIProvider` after the refactor

- It takes `quirks: Quirks`. `_is_codex_host` and `_is_opencode_host` go away;
  `_finalize_body` and `_headers` read the quirks.
- Each dropped or rewritten parameter is recorded on the request and emitted as
  `Degraded` (§6). Until phase 3, it is logged at debug level instead.
- `openai.py` is split along the existing seams: `openai_chat.py`
  (`_build_chat_body`, `_events_for_chat_chunk`), `openai_responses.py`,
  and `openai.py` (provider, `EndpointFallback`, shared helpers). This is for
  reading, not to meet a line cap.

### 4.5 Reasoning-effort clamping

Each adapter maps Nexus efforts (`none…max`) to its wire value today, partly in
`reasoning_effort_applied` (`openai.py`). Collect each adapter's mapping into one
table per adapter, with a test per row. Whenever the requested effort is clamped
or dropped, the adapter emits `Degraded`. Where every adapter maps today is not
yet surveyed.

## 5. Typed provider errors

Add to `nexus/errors.py`. All subclass `ProviderError`, carry `status_code` and
`retry_after`, and keep redacted, bounded details:

| Class | When |
| --- | --- |
| `ProviderAuthError` | 401/403 |
| `ProviderRateLimited` | 429 after retries are exhausted |
| `ProviderOverloaded` | exists; 529, or 503 with an overload body |
| `ContextLengthExceeded` | provider-specific body codes (`context_length_exceeded`, Anthropic "prompt is too long", Gemini equivalents) |
| `CapabilityRejected` | exists; gains a required `feature` |
| `InvalidRequest` | other 400/404/422 |
| `ProviderServerError` | 5xx after retries |
| `ProviderNetworkError` | connect, TLS or idle-read timeout |
| `MalformedStream` | bad SSE or NDJSON framing (`MalformedToolCall` stays separate) |

- Classification happens in one place, `nexus/model/provider_errors.py`
  (`classify(status, body, adapter) -> ProviderError`), called from `http.py`. Each
  adapter contributes a small table of body patterns, so the pattern-to-class
  table becomes data that is tested.
- `core/loop.py`: `_rejected_feature` reads `CapabilityRejected.feature` first.
  The substring table stays as a fallback for file and subprocess providers, and
  a debug log shows when it was used.
- `ContextLengthExceeded` gives the loop a typed hook for compaction-and-retry.
  Whether to use it is a separate decision. This plan only adds the type.
- Layering: `errors.py` stays the bottom layer and imports nothing.

## 6. New stream events (contract 3 change)

Add to `nexus/model/stream.py`:

- `Timing{connect_ms, first_byte_ms, first_token_ms, total_ms, chunks}`, measured in
  `http.py` and emitted by the adapter just before `MessageStop`.
- `Degraded{param, action, reason}`, for example
  `param="temperature", action="dropped", reason="codex rejects it"`. It is
  emitted before the first content event.

Through the stack:

- Recorded durably and reduced in `nexus/view/reduce.py`, so replay reproduces them.
- `ConversationView` exposes them on the assistant turn.
- Both UIs render them as labelled rows. Timing sits in the turn footer
  ("connect 120 ms · first token 640 ms · 3.2 s"). Degraded is a warning row that
  is never collapsed by default.
- Needs a `docs/decisions.md` entry, an update to `docs/events-and-view.md`, and a
  TUI PTY check plus an NSH browser check.

## 7. Cheap additions (preset row + registry mapping + fixture)

Each one is a `Preset`, an `_NPM_ADAPTERS`/`_DIRECT_ADAPTERS` entry where models.dev
names it, a `docs/provider-onboarding.md` inventory row, and an offline fixture.
Each is marked **not verified live** until a live smoke test runs against it.

- OpenRouter. Its `usage.cost` would be Nexus's first provider-reported cost, which
  needs a `Usage` field. Decide in phase 4.
- Groq, DeepSeek, Mistral (OpenAI-compatible endpoint), Together, Fireworks, Cerebras.
- Local servers: LM Studio, vLLM, llama.cpp `server` (keyless, loopback).

Deferred, because each needs real code: Azure OpenAI, AWS Bedrock, Google Vertex,
Cohere native.

## 8. Testing

- **Golden request bodies first (phase 0).** Before any refactor, snapshot the
  body and the non-secret headers each host produces today for a fixed set of
  requests: plain text, tools, images in tool results, reasoning effort, cache
  boundaries, session id. Commit them under
  `tests/fixtures/openai_presets/`. The refactor must keep them byte-identical,
  except where a difference is listed and approved in the phase-1 PR.
- The existing conformance harness (`tests/provider_conformance/`, `httpx.MockTransport`)
  runs unchanged against the refactored adapters.
- `tests/test_openai_presets.py`: every preset row is valid and `drop_params`
  names real body keys. Every `Degraded` reason is non-empty.
- `tests/test_provider_errors.py`: one case per class, per adapter pattern, using
  recorded error bodies. Also checks that no header or key-shaped string appears
  in `str()` or `repr()`.
- `Timing`: a fake clock in `http.py` tests. Reducer replay test for both events.
- Live: the existing gated tests, plus one gated case per §7 vendor.

## 9. Phases and exit criteria

| Phase | Work | Exit |
| --- | --- | --- |
| **0 — Baseline** | Golden bodies and headers for every current OpenAI-family host; survey where each adapter maps effort. | Goldens committed; survey noted in this plan. |
| **1 — Presets** | `openai_presets.py`; `OpenAIProvider` reads quirks; Codex, OpenCode Go, Copilot, xAI and NVIDIA become presets; split `openai.py`; `preset` config key; doctor/Settings show the preset. | Goldens unchanged; full suite passes; live Codex and Copilot smoke where gated. |
| **2 — Typed errors** | New classes, `classify`, adapter pattern tables, loop reads typed `feature`. | Error tests pass; the loop's substring fallback is not used by any built-in HTTP adapter in the test suite. |
| **3 — Events** | `Timing` and `Degraded` through stream, store, reducer, view and both UIs; effort clamp tables. | Decision entry; `events-and-view.md` updated; TUI PTY check and NSH browser check show both rows. |
| **4 — Vendors** | §7 presets; decide on OpenRouter cost. | One offline fixture per vendor; live status recorded per preset. |

Rough size: about 1.5–2.5k lines changed in Python, most of it tests and
fixtures. `openai.py` should shrink. This is an estimate, not measured.

## 10. Docs to update with the code

- `docs/models.md` and `docs/adding-a-provider.md`: "an OpenAI-compatible vendor is a preset row".
- `docs/provider-onboarding.md`: per-preset wire inventory and live status.
- `docs/config.md`: `preset`, bounded `extra_body`.
- `docs/events-and-view.md`, `docs/surfaces.md`: `Timing`, `Degraded`.
- `docs/loop.md`: typed errors and the substring fallback.
- `docs/decisions.md`: "providers stay in Python; Rust port deferred" (§11), and the events.
- `docs/module-map.md`: rows for `openai_presets.py`, `openai_chat.py`,
  `openai_responses.py`, `provider_errors.py`.

## 11. Deferred: a Rust engine

The earlier draft proposed `rust/llm-hub`: a Cargo workspace with a PyO3
extension, a tokio runtime bridged to asyncio, and a dual-engine conformance
harness. It is deferred. Revisit it only if one of these becomes true:

1. A benchmark shows the Python HTTP path costs something measurable per turn
   (CPU or time to first token). The `docs/devtools.md` benchmark can record this.
2. We decide to publish the provider layer as a standalone library for other
   projects.
3. A Rust process needs to call models without the daemon, which would first
   need a change to the "credentials stay in the daemon" rule.

If revisited, the preset table, typed errors and golden bodies from this plan
are the spec for the port.

## 12. Risks

- **Behaviour drift in the refactor.** `openai.py` holds live-observed quirks
  that are only partly in fixtures. Mitigations: goldens first, and any
  difference listed and approved explicitly.
- **Error misclassification.** A wrong class can change loop behaviour (retry,
  failover, degradation). Mitigations: recorded error bodies per class, and
  unknown bodies fall back to `ProviderError` exactly as today.
- **Event noise.** `Degraded` on every turn for a known host quirk could be
  noisy. Phase 3 decides whether a repeated identical `Degraded` is shown once
  per session, while still being recorded every time.

## 13. Open questions for the user

1. Do you approve the new stream events `Timing` and `Degraded` (phase 3)? They
   are a contract change visible in both UIs.
2. Should presets be chosen only explicitly (`preset = …`), or keep host
   detection as the default? The plan keeps detection for compatibility.
3. Which §7 vendors matter first?
4. Should OpenRouter's reported cost become a `Usage` field?
