# 14 — `nexus-llm-hub`: one Rust engine for every HTTP model provider

Status: **planning**. Nothing here is implemented or verified.
Inspiration: [smhanov/llmhub](https://github.com/smhanov/llmhub) (Go, MIT). We borrow its ideas, not its code.

## 1. Goal

Build `nexus-llm-hub`, a Rust library that gives every LLM provider one
interface: one request type, one stream-event enum, one error enum, and one
capability descriptor. It also has a Python interface (PyO3). Nexus uses it
through its existing `Provider` protocol (`nexus/model/provider.py`).

Scope for v1:

- **In:** the HTTP providers Nexus already speaks:
  - Anthropic Messages
  - OpenAI Responses and Chat Completions
  - Gemini
  - Ollama (native and OpenAI-compatible)
  - the OpenAI-family presets: Codex, GitHub Copilot, xAI, NVIDIA, OpenCode Go
- **In:** OpenAI-compatible vendors that cost only a preset entry (§7).
- **Out:**
  - subprocess and agent providers: `opencode` ACP, `claude-agent`, the `claude-code` worker, `antigravity`/`agy`
  - `scripted`
  - user file providers (`.agents/providers/*.py`)

  These stay in Python. They are not wire adapters.

Non-goals:

- Routing, the models.dev registry, tiers, the usage gate, sign-in flows,
  credential storage and token-count caching. All of these stay in Python
  (`nexus/model/router.py`, `registry.py`, `tiers.py`, `nexus/auth/*`).
- The hub is a **wire adapter**. It turns the IR into HTTP, and SSE/NDJSON into
  `StreamEvent`s. It does nothing else.

## 2. What we take from llmhub, and what we do differently

| llmhub idea | Adopt? | Notes |
| --- | --- | --- |
| Ordered content parts per message (text, reasoning, image, tool call) | Yes | Nexus already has this (`message.py`). The Rust types mirror it exactly. |
| Reasoning as a first-class part and stream delta | Yes | Plus `ThinkingEnd(signature)`. llmhub drops Anthropic signatures; we must replay them. |
| One `ThinkingLevel` enum that each provider maps, with documented clamping | Yes | Nexus efforts `none…max`. The clamp table lives in one Rust module and is tested. |
| Small provider trait plus a name-keyed registry | Yes | A Rust `Provider` trait. Cargo features replace Go blank-import tree-shaking. |
| One shared OpenAI-compatible core configured by a **quirks struct** | Yes, the main structural win | It replaces the accreted branches in `openai.py` (1586 lines) with declarative presets. |
| Normalised usage (cache read/write, reasoning tokens) | Yes | Same as Nexus `Usage`. |
| Timing metrics (connect, first byte, first token, chunks) | Yes, new | Exposed as an optional `Timing` event (§4.3). |
| Retry honours `Retry-After`, adds jitter, rebuilds the request | Yes | Using Nexus's policy: 4 attempts; retry on 408/409/425/429/5xx and network errors; never after the first event. llmhub's 429-only retry is too narrow. |
| HTTP errors surface before the stream starts, so callers can fail over | Yes | This is already Nexus's loop contract. |
| Cache on by default, with a per-conversation cache key mapped per provider | Yes | Driven by `metadata["cache"]` boundaries, as today. |
| `extra_body` escape hatch merged last | Yes, bounded | Per-provider-section config only. It is never set by the catalogue. |
| Functional options with no request struct | **No** | A serialisable `Request` struct that crosses FFI and can be logged and replayed. |
| Flat `StreamChunk` with optional fields | **No** | A tagged enum (already Nexus's `StreamEvent`). |
| Untyped string errors | **No** | A typed `HubError` enum (§4.4). |
| Unsupported options silently ignored | **No** | Every dropped parameter is reported (`Degraded` event, §4.3) so the user sees it. This is the Nexus "show everything" rule. |
| Gemini empty tool-call ids, key in query string, 1024 `max_tokens` default | **No** | Synthetic stable ids, the `x-goog-api-key` header, no silent output cap. |

## 3. Shape and placement

### 3.1 Repository layout

The crates live in the repo, not in a git submodule. The repo has no `.gitmodules` and
vendors everything else. "Sub-module" here means a self-contained component
with its own Cargo workspace and Python package boundary, which could be split
out later.

```
rust/llm-hub/
  Cargo.toml                 # [workspace]; members below; shared [workspace.dependencies]
  Cargo.lock
  NOTICE                     # credits llmhub for design ideas (MIT)
  README.md                  # crate-level contract; links docs/llm-hub.md
  crates/
    hub-core/                # nexus-llm-hub: IR types, Provider trait, errors, events, quirks, effort map
      src/{lib.rs, ir.rs, event.rs, error.rs, caps.rs, effort.rs, accumulate.rs, redact.rs}
    hub-http/                # transport: reqwest + rustls, retry policy, SSE + NDJSON decoders, timing
      src/{lib.rs, transport.rs, retry.rs, sse.rs, ndjson.rs, timing.rs}
    hub-providers/           # adapters, each behind a cargo feature
      src/{anthropic.rs, openai/{mod.rs, responses.rs, chat.rs, quirks.rs, presets.rs},
           gemini.rs, ollama.rs, registry.rs}
    hub-py/                  # PyO3 cdylib → nexus._llm_hub (abi3-py313)
      src/{lib.rs, convert.rs, stream.rs, runtime.rs}
    hub-cli/                 # `llm-hub` dev binary for live smoke checks (not shipped in the wheel)
  tests/fixtures -> reuses ../../tests/fixtures and tests/provider_conformance/fixtures by path
```

There are two reasons for a real Cargo workspace. It is the repo's first
multi-crate Rust unit, and `hub-core` must be usable with no HTTP or Python
dependency, so other Rust clients such as the TUI could reuse its types later.

### 3.2 Python side

```
nexus/_llm_hub.abi3.so            # built extension (setuptools-rust ext-module), optional
nexus/_llm_hub.pyi                # typed stub for the native module
nexus/model/providers/hub.py      # HubProvider: implements nexus.model.provider.Provider
nexus/model/providers/hub_codec.py# msgspec <-> native bytes codec, error mapping
```

`hub.py` imports only `nexus.model.*`, `nexus.errors` and `nexus.util`, so it
passes `tests/test_layering.py`. If `nexus._llm_hub` fails to import,
`HubProvider` is unavailable and routing silently keeps the Python adapter.
The fallback is logged once, and `nexus doctor` shows it.

### 3.3 FFI boundary: bytes, not objects

Python and Rust exchange **msgpack bytes** of the existing IR. `msgspec` encodes
`ModelRequest`, and Rust decodes it with `serde` (`rmp-serde`). Stream events come back
as msgpack frames, and `msgspec` decodes them into the existing `StreamEvent` structs.

Why:

- No per-field PyO3 conversion code.
- One schema, enforced by a parity test (§8.3).
- Requests can be dumped and replayed from either side.
- msgspec decoding is faster than building Python objects in Rust.

The tagged-union tags are the msgspec `tag` values already used in
`message.py` and `stream.py`.

### 3.4 Async bridge

- One process-wide tokio multi-thread runtime is created lazily. There are 2 worker
  threads by default, bounded and configurable via `NEXUS_LLM_HUB_THREADS`.
- `pyo3-async-runtimes` (tokio feature) turns Rust futures into asyncio awaitables.
- `HubStream` is a Python async iterator. `__anext__` awaits the next frame from a
  bounded `tokio::mpsc` channel with capacity 64. The bound provides backpressure, and the
  GIL is released while waiting.
- **Cancellation:** `aclose()`, or garbage collection of the iterator, drops the
  receiver. The producer task sees the closed channel and aborts the HTTP request.
  This is tested: no dangling connections after the loop cancels a turn.

## 4. The Rust contract

### 4.1 Provider trait

```rust
#[async_trait]
pub trait Provider: Send + Sync {
    fn name(&self) -> &str;
    fn capabilities(&self, model: &str) -> Capabilities;           // adapter-level defaults only
    async fn stream(&self, req: Request, auth: &dyn AuthSource)
        -> Result<EventStream, HubError>;                           // Err = failed before first event
    async fn count_tokens(&self, req: &Request, auth: &dyn AuthSource)
        -> Result<Option<u64>, HubError>;
}
```

`EventStream` is `Pin<Box<dyn Stream<Item = Result<Event, HubError>> + Send>>`. It always
ends with exactly one `MessageStop` or one `Err`, an llmhub guarantee that we enforce in a
wrapper rather than trusting each adapter.

### 4.2 IR (mirrors `nexus/model/message.py` and `request.py`)

- `Message { role: User|Assistant, content: Vec<Block> }`. `MessageMeta` is harness-only and is
  **not** sent across the boundary.
- `Block`: `Text`, `Thinking{text, signature}`, `Image{media_type, data|url}`, `Document`,
  `ToolUse{id, name, input}`, `ToolResult{tool_use_id, content, is_error}`. The harness-only fields
  `context_note`, `display`, `metrics` and `diff` are stripped by the Python codec.
- `Request { messages, system, tools: Vec<ToolSchema>, params: SamplingParams, model, metadata }`.
  `metadata` carries the cache boundaries and the session id, exactly as today.

### 4.3 Events (mirror `nexus/model/stream.py`, plus two additions)

- Existing events:
  - `MessageStart`, `TextDelta`, `ThinkingDelta`, `ThinkingEnd{signature}`
  - `ToolCallStart{id,name}`, `ToolCallDelta{id,partial_json}`, `ToolCallEnd{id,input}`
  - `Usage{input,output,cache_read,cache_write,reasoning}`, `MessageStop{stop_reason}`, `Raw`
- Proposed new events, adopted in phase 4 after a contract review:
  - `Timing{connect_ms, first_byte_ms, first_token_ms, total_ms, chunks}`
  - `Degraded{param, reason}` (for example "temperature dropped: host rejects it")
- Until phase 4, both new events are dropped in the codec, and the Python adapter
  behaviour is matched exactly. Adding events touches `docs/events-and-view.md` and the
  reducer. That is a contract-3 change and gets its own decision entry.
- `ToolCallAccumulator` is ported to Rust with the same semantics: calls are interleaved by id,
  and a duplicate id or invalid JSON raises `MalformedToolCall`.

### 4.4 Errors

```rust
pub enum HubError {
    Auth { status, detail },                 // 401/403
    RateLimited { status, retry_after },     // 429 after retries exhausted
    Overloaded { status, retry_after },      // 529 / 503 overloaded → nexus ProviderOverloaded
    ContextLength { detail },                // provider-specific body patterns, matched in Rust
    CapabilityRejected { feature },          // → nexus CapabilityRejected (one degraded retry)
    InvalidRequest { status, detail },
    Server { status, detail },
    Network { detail },                      // connect / TLS / idle-read timeout
    MalformedStream { detail },              // bad SSE, bad tool JSON
    Cancelled,
}
```

- The detail is bounded to 500 characters and secret-redacted **in Rust**, using a port of the
  `redact_secrets` patterns. Python applies `redact_secrets` again. URLs pass through the
  `_safe_url` rule, so no query and no userinfo.
- The Python codec maps every variant onto the existing `nexus.errors` classes and sets
  `status_code`, so the loop is unchanged in phases 1–3.
- In phase 4 the typed variants let `core/loop.py` stop substring-matching
  (`_rejected_feature`).

### 4.5 Auth: credentials stay in the daemon, and OAuth stays in Python

The hub never reads `~/.nexus/credentials.json` and never runs a sign-in.

- `AuthSource` is a trait with two implementations:
  - `StaticHeaders` is used by the standalone Rust/CLI path, with a key from an env var.
  - `PyHeaderFactory` wraps the existing Python async header factories (`ChatGPTOAuthHeaders`,
    `CopilotHeaders`, `XAIHeaders`, `StoredKeyAuth`, `${env:…}` resolution). Rust awaits it
    **once per attempt**, so a refreshed OAuth token is picked up on retry.
- Headers live only in memory for the life of the request. They are never logged, never in
  `Debug` output (a newtype with a redacted `Debug`), and never echoed in errors.
- This keeps Codex, Copilot and xAI usable through the hub from phase 2. Only their header
  production stays in Python.

### 4.6 Transport

- `reqwest` with `rustls`. Use `rustls-platform-verifier`, so corporate root CAs work as they do with httpx,
  and avoid OpenSSL, so manylinux and musllinux builds need nothing extra.
- Honour `HTTP(S)_PROXY` and `NO_PROXY`, as httpx does.
- Timeouts match `DEFAULT_TIMEOUT`: 10 s connect, and 300 s **idle** read between chunks rather than total.
- Retry: 4 attempts, 0.5 s base, 30 s cap, 25% jitter, and `Retry-After` as seconds or an HTTP date.
  Retry only before the first event is yielded, never mid-stream.
- Bounds:
  - max SSE line of 1 MiB
  - max event of 8 MiB
  - max error body read of 64 KiB
  - all configurable, with the limits documented

## 5. OpenAI family: quirks as data

`openai.py` carries host rules as branches. In the hub every host is a
`Preset`:

```rust
pub struct Quirks {
    pub dialects: &'static [Dialect],          // [Responses, Chat] with EndpointFallback learning
    pub drop_params: &'static [Param],         // Codex: temperature, top_p
    pub cache_key: CacheKeyMode,               // BodyPromptCacheKey | Header("x-grok-conv-id") | Header("x-opencode-session") | None
    pub session_header: Option<&'static str>,
    pub static_headers: &'static [(&'static str, &'static str)], // x-grok-client-*, X-BILLING-INVOKE-ORIGIN, originator
    pub reasoning_field: ReasoningField,       // reasoning_content | reasoning | summary items
    pub store: Option<bool>,                   // xAI store:false
    pub tool_result_images: ImageMode,         // inline | to_text | drop
    pub token_count: TokenCountMode,           // ResponsesInputTokens | None
    pub base_url_normalize: fn(&str) -> String,
}
```

Each dropped parameter emits `Degraded` (§4.3), which is shown to the user.
`EndpointFallback` keeps its learned per-model dialect in memory. Its Python behaviour,
which also persists nothing, is matched.

## 6. Providers: order and difficulty

| # | Provider | Wire | Difficulty | Notes |
| --- | --- | --- | --- | --- |
| 1 | `anthropic` | Messages + `count_tokens` | Easy | Cache-control placement (last tool, otherwise system, plus the history boundary), same-role coalescing, `usage_input_excludes_cache`, thinking signatures. The best fixture coverage. |
| 2 | `ollama` | `/api/chat` NDJSON and `/v1` SSE | Easy | Keyless, loopback default. |
| 3 | `gemini` | `streamGenerateContent` + `countTokens` | Easy | Synthetic stable tool ids, refusal fixture, `x-goog-api-key` header. |
| 4 | `openai` Responses + Chat | both dialects | Medium | `EndpointFallback`, tool-result images, `/v1/responses/input_tokens`. |
| 5 | `nvidia` | Chat preset | Easy | `reasoning_content` becomes thinking. Not verified live today. |
| 6 | OpenCode Go | Chat preset + fallback | Easy | Session header. |
| 7 | `github-copilot` | Chat → Responses fallback | Medium | Headers from Python. |
| 8 | `codex` | Responses at chatgpt.com | Medium | Drops temperature and top_p; `prompt_cache_key` and `session_id`; OAuth headers from Python. |
| 9 | `xai` | Responses via the Grok CLI proxy | Medium | Proxy headers, `store:false`. Not verified live today, and it stays flagged so. |

Stays in Python permanently:

- `opencode` ACP
- `claude-agent`
- the `claude-code` worker
- `antigravity`
- `scripted`
- the dev mock
- file providers

## 7. Cheap additions (preset rows plus a fixture, no new code paths)

Once §5 exists, each of these is a `Preset`, a models.dev `@ai-sdk/*` → kind
mapping in `_NPM_ADAPTERS`, an onboarding inventory row (`docs/provider-onboarding.md`),
and an offline fixture. Each is marked **not verified live** until someone runs `hub-cli` against it.

- OpenRouter (provider-reported `usage.cost` is surfaced; it becomes Nexus's first provider-reported cost)
- Groq, DeepSeek, Mistral (OpenAI-compatible endpoint), Together, Fireworks, Cerebras
- Local servers: LM Studio, vLLM, llama.cpp `server` (keyless, loopback)

Deferred, because each is not "easy":

- Azure OpenAI: deployment-based URLs and `api-version`.
- AWS Bedrock: SigV4 and the event-stream binary framing.
- Google Vertex: ADC/OAuth.
- Cohere native.

## 8. Testing

### 8.1 Rust (offline, `cargo test --locked` in `rust/llm-hub`)

- Each adapter has decoder tests against the **existing** recorded fixtures:
  - `tests/fixtures/anthropic/*.sse`
  - `tests/fixtures/gemini/*.sse`
  - `tests/fixtures/ollama/*`
  - `tests/provider_conformance/fixtures/*`

  The fixtures are read by relative path, not copied. Both engines are judged against the same bytes.
- Each adapter has request-body golden tests. The expected JSON is produced once by the Python
  adapters' `build_request_body` and committed, so a body difference is a test failure.
- Transport tests use a `wiremock` local server:
  - retry statuses and `Retry-After`
  - no retry after the first event
  - the idle timeout
  - cancellation aborting the connection
  - the size bounds
- Redaction tests check that no header value or key-shaped string appears in any `HubError` `Display` or `Debug`.
- Effort-clamp table tests for each provider.

### 8.2 Python (offline, part of the normal `pytest -q`)

`httpx.MockTransport` cannot intercept Rust. So
`tests/provider_conformance/harness.py` gains a **loopback fixture server** mode.
It is a small asyncio HTTP server on `127.0.0.1:0` that replays the same fixtures,
and the hub provider is pointed at it with `base_url`.

- Every conformance spec runs twice, once per engine: `engine=python` and `engine=hub`.
- The stream event sequences must match exactly, until the phase-4 events are adopted.
- The tests skip cleanly when `nexus._llm_hub` is not built, because CI sets `CARGO=false` for the Python job.
  A dedicated CI job builds the extension and runs the hub half (§9).

### 8.3 Schema parity

`tests/test_llm_hub_schema.py` compares two schemas, and any drift fails:

- `msgspec.json.schema` of `ModelRequest` and `StreamEvent`
- a schema exported by `nexus._llm_hub.schema()`, generated with `schemars` from the Rust types

### 8.4 Live (gated, deselected by default)

`tests/test_llm_hub_live.py` has one case per provider, reusing the existing env gates
(`ANTHROPIC_API_KEY`, `NEXUS_XAI_LIVE`, …).

`hub-cli` covers manual checks. The commands are `llm-hub chat --provider anthropic --model … --stream --tools`,
modelled on llmhub's `examples/cli`. Each preset's live status is recorded in `docs/llm-hub.md`.

## 9. Build, CI, release

- **Build:** stay on `setuptools-rust`. A switch to maturin would rewrite the build and put the
  existing bins at risk. Add the following to `pyproject.toml`:
  `[[tool.setuptools-rust.ext-modules]] target="nexus._llm_hub", path="rust/llm-hub/crates/hub-py/Cargo.toml", binding="PyO3", py-limited-api="cp313", optional=true, args=["--locked"]`.
  The `hub-cli` crate is **not** a wheel bin.
- **MANIFEST.in:** add the workspace's Cargo files and `crates/*/src/**/*.rs`. Also fix the
  existing sdist gap: `rust/widgets` is missing. **.gitignore:** add `rust/llm-hub/target/`.
- **CI (`ci.yml`):**
  - Add `rust/llm-hub/` to the `changes` path filter.
  - Add a new `llm-hub` job: `cargo test --locked`, `cargo clippy -D warnings`, and `cargo fmt --check`. The new
    workspace starts rustfmt-clean, unlike the older crates.
  - Add a `python-hub` job: build the extension with `CARGO` enabled, then run the conformance suite with both engines.
- **Wheels (`native-wheels.yml`):** extend `CIBW_TEST_COMMAND` with
  `python -c "import nexus._llm_hub as h; print(h.version())"`, because `optional=true` would
  otherwise hide a failed build.
- **Risk:** the abi3 extension sits next to the per-cpython bins, so check how
  setuptools-rust tags the wheel and update the `release.yml` asset assertions to match.
  Not verified.
- **Toolchain:** `rust-version = "1.97"`, matching CI. Versions: the crate stays `0.1.0` and is independent of
  the Python version, which release-please already leaves alone.

## 10. Rollout in Nexus

Each provider section gets a new config key, `engine = "python" | "hub"`, documented in
`docs/config.md`. The default is `"python"`, with `NEXUS_LLM_ENGINE` as the global override.

- `Runtime._construct_provider` (`runtime.py:~6100`) picks `HubProvider` when the engine is `hub`
  and the extension imports. Otherwise it keeps the Python adapter and records why.
- `nexus doctor` and the Settings provider rows show the active engine per provider and the reason for
  any fallback.

The flip is per provider. The default changes to `hub` only after all three of these hold:

1. the dual-engine conformance tests pass
2. the live smoke test passes, where a live gate exists
3. one week of dogfooding without a hub-only regression

Python adapters are deleted only by a later, separate decision.

## 11. Phases and exit criteria

| Phase | Work | Exit |
| --- | --- | --- |
| **0 — Decision and skeleton** | Add a `docs/decisions.md` entry superseding "providers stay in Python" (~L437, L532) and "no PyO3" (Ratatui boundary). Add the empty workspace, `hub-core` types, the `hub-py` `version()`/`schema()` functions, packaging, CI jobs, the wheel smoke test, `docs/llm-hub.md` (linked from `docs/README.md` and `AGENTS.md`), and module-map rows. | Wheels on all four platforms import `nexus._llm_hub`; the schema parity test passes. |
| **1 — Transport and Anthropic** | `hub-http` (retry, SSE, timing, bounds, redaction); the Anthropic adapter plus `count_tokens`; the `HubProvider` codec and error mapping; the loopback harness; `engine` config. | The Anthropic conformance tests pass on both engines with identical events; the cancellation test passes; the live Anthropic smoke test passes. |
| **2 — Ollama, Gemini, OpenAI core** | Native and compat Ollama, Gemini, OpenAI Responses and Chat, the `Quirks`/`Preset` system, `PyHeaderFactory`. | Conformance for every spec in `tests/provider_conformance` passes on both engines. |
| **3 — Presets** | NVIDIA, OpenCode Go, Copilot, Codex, xAI as presets; then the §7 additions. | Golden request bodies match the Python adapters; per-preset live status is recorded. |
| **4 — Contract upgrades** | Adopt the `Timing`/`Degraded` events through the reducer and view (both UIs show them as labelled rows); typed errors replace loop substring matching; flip defaults per §10. | A decision entry, updated `events-and-view.md`, and a TUI PTY check showing the timing and degraded rows. |
| **5 — Optional** | Publish `nexus-llm-hub` standalone (crates.io plus a separate `maturin` PyPI wheel exposing a non-Nexus Python API); consider reusing `hub-core` types in the TUI. | Separate approval. |

Rough size: about 6–8k lines of Rust for phases 0–3, using the Python adapters as a size
proxy, plus about 600 lines of Python glue and tests. This is an estimate, not measured.

## 12. Docs to update with the code

- `docs/llm-hub.md` (new): contract, FFI format, the engine switch, the live-verification ledger.
- `docs/models.md` and `docs/adding-a-provider.md`: "adding an OpenAI-compatible vendor = a preset".
- `docs/provider-onboarding.md`: per-preset wire inventory.
- `docs/config.md` (`engine`, `NEXUS_LLM_ENGINE`, `NEXUS_LLM_HUB_THREADS`), `docs/architecture.md`
  (the Python/Rust split now includes providers), `docs/security.md` (headers in Rust memory,
  rustls, redaction), `docs/testing.md` (loopback harness, dual engine), `docs/release.md` (abi3).
- `docs/module-map.md`: rows for `hub.py`, `hub_codec.py`, the `.pyi`, and `rust/llm-hub/` in the repository layout.

## 13. Risks

- **Behaviour drift during the port.** `openai.py` holds live-observed quirks that are only partly in
  fixtures. Mitigations: golden request bodies, dual-engine conformance, a per-provider flip, and a Python fallback.
- **Two event loops.** asyncio and tokio interplay can deadlock or leak. Mitigations: one runtime, bounded
  channels, never block the asyncio thread, and stress tests that cancel a turn.
- **Build fragility.** This is the first PyO3 extension, it adds an abi3 wheel tag, and it pulls in the reqwest and rustls trees.
  Mitigations: `optional=true` during phases 0–3, the wheel smoke test, and pure rustls.
- **Security regressions.** Mitigations: redaction is applied in both languages, header values never reach Rust `Debug`, and the
  existing secrets conformance tag runs against the hub.
- **Benefit is not proven.** Python's httpx path is not a measured bottleneck. The real wins are one
  declarative preset table, typed errors, and a reusable Rust core. A phase-1 benchmark records TTFT and
  CPU per turn for both engines (`docs/devtools.md` benchmark), so the decision rests on data.

## 14. Open questions for the user

1. Should the hub also be **published standalone** (phase 5), or stay internal to the Nexus wheel?
   The answer decides whether the Python API is shaped for outside users now.
2. Is "sub-module" meant literally as a **git submodule** (a separate repo)? This plan assumes an
   in-repo workspace, which fits the repo's vendor-everything practice and needs no CI checkout changes.
3. Should the **eventual goal be to delete** the Python HTTP adapters, or to keep both engines long-term?
4. Do you approve the new stream events `Timing` and `Degraded` (phase 4)? They are a contract change visible in both UIs.
