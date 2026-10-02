# Feasibility: converting Nexus entirely to Rust

Assessment date: 2026-10-02. Status: **proposal, not an approved migration**.
Repository baseline: `nexus-harness` 0.2.15, Python ≥3.13, Textual 8.2.8.
This report changes documentation only. No Rust prototype or benchmark has
been run. Crate suitability, performance projections, and effort estimates below
are **not verified**.

Companion report: [ratatui-feasibility.md](../docs/ratatui-feasibility.md) assesses
replacing only the terminal UI. This report assesses replacing everything: the
daemon, harness, providers, tools, storage, CLI, and TUI. The browser client
stays HTML/CSS/JS in both cases.

## 1. Recommendation

A complete Rust conversion is **technically feasible**. Nexus has no
computation that Rust cannot express, and nearly every Python dependency has a
mature Rust counterpart. The architecture also helps: five explicit contracts
([architecture.md](../docs/architecture.md#the-five-contracts)), a single host
protocol, an append-only event log, and a pure reducer are all good porting
seams.

It is **not recommended as a single rewrite**, and probably not recommended at
all unless the goals in §2 are agreed priorities. The expected cost is roughly
**2.2–3.2 engineer-years** (§9). Feature work would stop or be duplicated
during that time. The Python test suite (≈100k lines) does not carry over. Three
features depend on Python itself:

1. **Code extensions**: user `.py` tools, Python hooks, and file providers are a
   documented public contract ([extending.md](../docs/extending.md)). A Rust daemon
   cannot import them without running Python.
2. **Claude subscription provider**: built on the Python `claude-agent-sdk`.
3. **Local voice**: built on Kestrel/PyTorch (Parakeet).

Each of these can stay behind a process boundary as a small Python sidecar, so
they block a *pure* Rust binary, not a Rust port.

If the team proceeds, the recommended route is a **strangler migration behind
the host protocol** (§8). First freeze the protocol and the session-record
schema, and build a black-box conformance suite that any daemon must pass.
Then rebuild the daemon in Rust layer by layer. Python stays only as optional
out-of-process sidecars for extensions, the Claude Agent SDK, voice, and
document conversion. A Ratatui TUI built as recommended in the companion report
moves over directly if its presentation crate stays transport-neutral.

| Question | Assessment |
| --- | --- |
| Can everything be expressed in Rust? | Yes. No subsystem needs Python semantics except Python extensions. |
| Can it ship as one static binary with no Python? | Only if Python code extensions, the Claude Agent SDK provider, voice, and AnyDoc become optional sidecars or are dropped. |
| Do existing `~/.nexus/nexus.db` sessions survive? | Yes, if the record schema and event JSON stay byte-compatible. This must be tested (§6). |
| Do existing user `.py` tools and hooks keep working? | Only through a Python extension host (§5.1). Otherwise this is a breaking change. |
| Will turns get faster? | Not much. Turn time is dominated by model latency and tool execution. |
| What improves noticeably? | Install and distribution, cold start, memory per daemon, TUI responsiveness, and concurrency headroom. None of this has been measured. |
| Is the test suite reusable? | Mostly no. Recorded fixtures, mock scenarios, and the Playwright web check are reusable. |
| Is a staged migration practical? | Yes, behind the existing host protocol, with a conformance suite as the gate. |

## 2. Why one would do this, and what it does not fix

Plausible benefits, all **not verified**:

- **Distribution.** Today an install needs Python ≥3.13, a venv, and native
  wheels for `sounddevice`, `msgspec`, and Kestrel; voice already has to be
  excluded on musl. A Rust build produces one self-contained executable per
  target. `install.sh`, `install.ps1`, and `nexus update` become a binary
  download. This is the strongest argument.
- **Cold start.** `nexus run` and daemon auto-start pay for interpreter startup
  and imports (Textual, httpx, msgspec, mcp). A Rust binary starts in
  milliseconds.
- **Memory.** One Python daemon per workspace is the problem
  [`plans/SHARED_DAEMON_PLAN.md`](../plans/SHARED_DAEMON_PLAN.md) addresses.
  A Rust daemon would likely be smaller still. However, the shared daemon fixes
  most of the cost on its own, so do it first and measure again.
- **Concurrency and robustness.** Tokio plus ownership rules give stronger
  guarantees for the per-session locks, shell jobs, MCP children, and bounded
  queues. These areas are currently handled by careful asyncio code.
- **TUI rendering.** See the companion report. The full port removes its
  PyO3 bridge and GIL concerns, because the TUI can talk to the daemon directly.

What it does not fix:

- Model latency, provider rate limits, tool run time, and context-assembly
  decisions.
- Behavioral bugs. A rewrite reintroduces solved bugs; the git history
  contains many fixes encoded only in Python tests.
- Extensibility. Python is the language users write extensions in. Rust makes
  that harder, not easier (§5.1).

## 3. Inventory

Line counts are from 2026-10-02 (`.py` unless noted). Difficulty is relative to
idiomatic Rust, not to the size alone.

| Area | Files | Lines | Rust difficulty | Notes |
| --- | ---: | ---: | --- | --- |
| `config/`, `errors.py`, `events.py`, `util.py` | 7 | 1,904 | Low | serde + `toml`; env interpolation; redaction. |
| `view/` (reducer, `ConversationView`) | 4 | 2,250 | Medium | Pure; must replay byte-identically (§6). |
| `model/` + `model/providers/` | 24 | 9,987 | Medium–High | Anthropic, OpenAI, Gemini, Ollama, OpenCode, scripted, Claude Agent SDK; registry; SSE; partial-JSON tool calls. |
| `auth/` | 5 | 1,026 | Medium | ChatGPT/Codex OAuth, Copilot, API keys, keychain/file store. |
| `core/` (loop) | 7 | 3,248 | Medium | Protocol-only loop; cancellation and limits. |
| `context/` | 7 | 4,345 | Medium | Budget, compaction, cache breakpoints, heuristic tokenizer. |
| `session/` | 10 | 4,716 | Medium | SQLite WAL, locks, fork, export; schema must stay compatible. |
| `tools/` + `tools/builtin/` | 35 | 16,528 | Medium–High | Permissions grammar, bundles, bash jobs, apply_patch, grep, webfetch (HTML→Markdown), websearch. |
| `agents/` | 7 | 7,576 | Medium | Subagents, git worktrees, review and integrate. |
| `ext/`, `skills/`, `hooks/` | 16 | 10,975 | **High** | Quarantine and generation-stamped hot loading of Python code (§5.1). |
| `mcp/` | 5 | 5,835 | Medium | Already a native stdio/HTTP/SSE implementation; `rmcp` is an option. |
| `runtime.py` | 1 | 5,371 | Medium | The composition root; tightly coupled to everything above. |
| `host/` + transports | 13 | 7,387 | Medium | 137 protocol classes, daemon, supervisor, UDS, HTTP/SSE, presence, doctor. |
| `host_support/` | 24 | 4,237 | Low–Medium | Search-server compose files and helpers. |
| `net/`, `observability/`, `client/` | 9 | 2,374 | Low | Outbound policy, logging, client. |
| `voice/` | 6 | 880 | **High** (engine) | Python adapter over Kestrel/PyTorch; audio capture (§5.3). |
| `cli.py`, `ui/cli/` | 10 | 2,733 | Low | `clap`; slash-command specs. |
| `ui/tui/` + `ui_support/` (+ 2,391 lines TCSS) | 48 | 13,834 | High | Full Ratatui rewrite; see companion report. |
| `devtools/` | 23 | 1,500 | Low | Mock scenarios. |
| `ui/web/` (JS/CSS/HTML) | 19 | 4,280 | None | Stays as is; served by the Rust HTTP layer. |
| **Total Python in `nexus/`** | ≈270 | ≈130,000 | | |
| `tests/` | 258 | ≈100,000 | Rebuild | Mostly white-box against Python APIs (§7). |

## 4. Dependency mapping

Crate names are candidates, not evaluated choices.

| Python | Purpose | Rust candidate | Risk |
| --- | --- | --- | --- |
| `asyncio` | Concurrency | `tokio` | Low; cancellation semantics differ (§5.4). |
| `msgspec` | Frozen structs, JSON | `serde`, `serde_json` | Low; field order and float formatting must match stored JSON. |
| `httpx`/`httpcore` | HTTP, pooling, SSE | `reqwest` or `hyper`, `eventsource-stream` | Low. |
| `sqlite3` | Session DB | `rusqlite` (bundled SQLite) | Low; keep identical PRAGMAs (`WAL`, `synchronous=FULL`, `busy_timeout=5000`). |
| `tomllib` | Config | `toml` | Low. |
| `fcntl` locks | Profile and session locks | `fs4` / `rustix` | Low; must interoperate with Python processes during the transition. |
| `difflib` | Diffs | `similar` | Low; output must not change in user-visible ways. |
| `html`, internal HTML→Markdown | webfetch | `html5ever`/`scraper` + custom | Medium; output parity affects model context. |
| `argparse` | CLI | `clap` | Low. |
| `keyring` + file store | Credentials | `keyring` crate + same file store | Medium; macOS Keychain access lists are per executable, so a new binary will prompt again. |
| `mcp` (optional) / native client | MCP | `rmcp` or port the native client | Low–Medium. |
| `textual`, `textual-diff-view`, `rich` | TUI | `ratatui`, `crossterm`, custom editor/Markdown/diff | High; see companion report. |
| `claude-agent-sdk` | Claude subscription provider | No official Rust SDK known (not verified) | High; §5.2. |
| `firecrawl-anydoc` | Document→Markdown | Keep the existing worker subprocess | Low if kept as a sidecar. It already uses a framed stdin/stdout protocol. |
| `sounddevice` | Audio capture | `cpal` | Medium. |
| `kestrel` / PyTorch (Parakeet) | Local dictation | `candle` or `ort` (ONNX) port, or keep a Python sidecar | High; §5.3. |
| `importlib`, `ast`, `inspect` | Extension quarantine and loading | None; replaced by an extension host | High; §5.1. |

## 5. Hard problems

### 5.1 Python code extensions

Nexus loads user Python at runtime: `.agents/tools/*.py`, `.agents/hooks/*.py`,
`.agents/providers/*.py`, and skill-bundled `tools/*.py`. They go through
quarantine and are imported under generation-stamped module names, so in-flight
calls keep their own generation (`nexus/tools/loader.py`, `nexus/ext/`).
`examples/python_api.py` also documents in-process use of `HostFacade` as a
library. This is the most important decision in the port.

| Option | How | User impact | Cost |
| --- | --- | --- | --- |
| A. Embed CPython (PyO3) | Daemon links libpython and imports extensions in-process | Contract preserved | Gives up the main benefit: you still ship Python. GIL and crash coupling in the daemon. |
| B. Out-of-process Python extension host | Rust spawns `python -m nexus_ext_host` per workspace generation and talks to it over a framed JSON-RPC protocol (tool specs, `run`, hook decisions, provider streams) | Contract preserved for users who have Python. Code extensions become optional. | Medium. A generation maps to a host process, which makes reload and retirement simpler than module juggling. |
| C. WebAssembly components | `wasmtime` runs WASM tools and hooks with explicit capabilities | Breaking change; better sandboxing; any language | High; new SDK and documentation. |
| D. Drop code extensions | Keep data extensions, command hooks (`hooks.toml`), MCP servers, and config providers | Breaking change; `.py` tools become MCP servers | Low to build, high in user trust. |

**Recommended: B, with D's mechanisms promoted as the primary path.** MCP and
command hooks already cover "new capability" and "enforce a rule" without
in-process code. The extension host keeps existing `.py` files working, and its
protocol can be close to MCP's tool shape. The `ToolContext` services passed
today (`emit`, `workspace`, `session_id`, narrow services) become RPC calls.
The trust note ([extending.md](../docs/extending.md#the-trust-note)) still applies; a
separate process is isolation from crashes, not a sandbox.

The in-process Python API (`HostFacade`) would be replaced by a client library
over the host protocol (Python client package or PyO3 bindings to the Rust
client). That is a breaking change for anyone embedding Nexus.

### 5.2 Claude subscription provider

`claude_agent.py` starts an isolated worker that drives the Python Agent SDK,
which in turn drives the Claude Code CLI. Options:

- Keep `_claude_agent_worker.py` as a Python sidecar. Rust already treats it as
  a subprocess with a byte-bounded protocol. Lowest risk; requires Python for
  this provider only.
- Drive the Claude Code CLI's streaming JSON mode directly from Rust. This
  removes Python but re-implements an SDK protocol that Nexus does not own and
  that may change with SDK releases. Not verified.

Recommended: sidecar first, direct driver only if the protocol proves stable.

### 5.3 Voice

The engine uses Kestrel's Parakeet runtime on PyTorch with a verified local
checkpoint. Porting inference to `candle` or ONNX Runtime means re-implementing
or exporting the model and its decoder and re-validating accuracy. That is a
machine-learning project, not a port. Keep voice as an optional Python sidecar
(it is already optional and excluded on musl). Audio capture can move to `cpal`
only if the sidecar takes PCM over a pipe, which is not required.

### 5.4 Cancellation, limits, and ordering

The loop and tools rely on asyncio cancellation reaching every await point,
with bounded cleanup (process-group kill for shell jobs and MCP children,
deadlines everywhere). Tokio cancels by dropping futures, so cleanup that
currently lives in `finally` blocks must move to explicit guards or
cancellation tokens. Every `asyncio.shield`, `TaskGroup`, and timeout in
`core/`, `tools/builtin/_jobs.py`, `mcp/client.py`, and `host/` needs an
explicit Rust equivalent and a test. This is where a port most easily produces
subtle regressions.

### 5.5 Behavior that only exists in code

Much of Nexus's value is accumulated detail: permission-rule grammar,
`apply_patch` parsing and staging, compaction thresholds, cache-breakpoint
placement, redaction rules, clipping announcements, provider quirks, and HTML
to Markdown output. Each changes the model's context or the user's view if
re-implemented slightly differently. Treat each as a golden-output parity
target, not a rewrite (§7).

## 6. Data and protocol compatibility

The contracts that must stay stable across the migration:

1. **Session records** in `~/.nexus/nexus.db`: table schema, event `type`
   names, `data` JSON shape, monotonic `seq`. A Rust daemon must read sessions
   written by Python and the reverse, while both exist.
2. **Replay equivalence**: the Rust reducer, applied to recorded sessions,
   must produce a `ConversationView` equal to Python's. Build this as a
   golden test: export real and mock sessions to JSONL, reduce in both, compare
   serialized views.
3. **Host protocol**: 137 command/result/event classes in
   `nexus/host/protocol.py`, over UDS and HTTP/SSE. Generate a schema (JSON
   Schema or a single IDL) from the Python definitions first, then generate or
   check Rust types against it. The browser client depends on this unchanged.
4. **Config and on-disk layout**: `nexus.toml`, `.agents/`, legacy `.nexus/`
   fallback, `~/.nexus/` machine state ([config.md](../docs/config.md)).
5. **Locks**: file locks must exclude correctly between a Python process and a
   Rust process during the transition.

Floating-point and key-order differences between `msgspec` and `serde_json`
matter where JSON is hashed, cached, or compared (prompt-cache prefixes,
quarantine hashes, export). Audit each.

## 7. Testing strategy

The current suite mostly imports Python modules and asserts on Python objects.
It verifies the Python implementation, not the behavior through a boundary,
so it cannot test a Rust daemon. What carries over:

- Recorded provider fixtures (`tests/fixtures/anthropic`, `gemini`, `ollama`,
  `opencode`, `models.dev`) as golden inputs for adapters.
- Mock scenarios (`nexus/devtools/mock/`) if the scenario format is
  language-neutral.
- `tests/playwright_web_check.py`, because it drives the real browser client
  over the host protocol.
- Extension fixtures (`tests/fixtures/extensions/`) for the extension host.

Required before porting begins:

1. **A host-protocol conformance suite**, written in Python against a running
   daemon over UDS and HTTP/SSE, using `ScriptedProvider` equivalents and
   temporary workspaces. It must pass against the Python daemon first. This is
   the migration gate: a Rust daemon is "done" for a layer when the suite passes.
2. **Golden outputs** for the behavior in §5.5: rendered prompts per provider,
   permission decisions, patch application, compaction, tool-detail
   presentation, HTML→Markdown.
3. **Replay equivalence** (§6.2).

Then add Rust unit tests per crate, PTY tests for the TUI (companion report
§9), and keep the layering rule as Cargo crate boundaries (§8). A crate graph
enforces one-way layering more strictly than `tests/test_layering.py` does.

## 8. Proposed architecture and staged migration

### Crate layout

Mirrors the Python layering, so `cargo` enforces it.

```text
crates/
  nexus-types      # config, errors, events, message IR, stream events, tool spec (contracts 1–5)
  nexus-view       # reducer, ConversationView
  nexus-model      # provider trait, HTTP/SSE transport, adapters, registry, auth
  nexus-core       # loop; depends only on traits
  nexus-context    # assembly, budget, compaction, caching
  nexus-session    # SQLite records, locks, export
  nexus-tools      # permissions, bundles, builtins, shell jobs
  nexus-ext        # skills, hooks, MCP, extension-host client
  nexus-agents     # subagents, worktrees
  nexus-runtime    # composition root
  nexus-host       # protocol, facade, daemon, UDS + HTTP/SSE (axum), web assets (embedded)
  nexus-tui        # Ratatui shell; depends on nexus-host protocol types and nexus-view only
  nexus-cli        # binary: `nexus`
sidecars/          # optional Python: extension host, claude-agent worker, voice, anydoc
```

Rule 2 in AGENTS.md (UI encapsulation) becomes: `nexus-tui` may depend only on
`nexus-types`, `nexus-view`, and the host client.

### Stages

The Python daemon stays the default until each stage passes its gate. Because
the host protocol is the seam, the Python CLI, Textual TUI, and browser client
can run against a Rust daemon from stage 3 onward.

| Stage | Deliverable | Gate |
| --- | --- | --- |
| 0. Freeze and measure | Protocol schema, record schema, conformance suite, golden outputs, baselines for startup, memory, turn overhead | Suite green on Python daemon. Decide §5.1 and the support matrix. |
| 1. Foundations | `nexus-types`, `nexus-view`, `nexus-session` | Replay equivalence on real exported sessions; Python and Rust read each other's DB. |
| 2. Model layer | HTTP/SSE, Anthropic, OpenAI, Gemini, Ollama, OpenCode, registry, auth, sign-in | Adapter goldens on recorded fixtures; live tests per provider. |
| 3. Daemon vertical slice | Loop, context, core tools, permissions, runtime, host over UDS | Conformance suite core journeys pass with the Python CLI as client. |
| 4. Full harness | All builtins, shell jobs, patch, agents/worktrees, skills, hooks, MCP, extension host, Claude Agent sidecar, HTTP/SSE + web | Full conformance suite and Playwright web check pass. |
| 5. TUI and CLI | Ratatui shell and `clap` CLI in Rust | Feature inventory from the companion report; PTY tests. |
| 6. Release | Binary matrix, installers, `nexus update`, docs, Python package retired or reduced to sidecars | Clean installs on every supported target. |

Stage 5 can start in parallel with stage 2 if the Ratatui TUI is first built as
described in the companion report, with its presentation crate independent of
the PyO3 bridge.

### Packaging and release

- Build targets: macOS arm64/x86_64, Linux glibc and musl x86_64/arm64, and
  Windows if `install.ps1` stays a supported promise. The UDS transport needs
  a Windows answer (named pipes or loopback TCP with token) regardless of
  language.
- `cargo-dist` or a custom matrix in `release.yml`; release-please supports
  Rust/Cargo workspaces, so Conventional Commits and version authority stay.
  Version files must still never be edited by hand.
- Web assets embedded in the binary (`include_dir`/`rust-embed`).
- Sidecars ship as an optional Python package (for example `nexus-sidecars`
  on PyPI) that the binary finds on `PATH` or a configured interpreter.
- macOS Keychain items are tied to the executable's identity. Code-sign the
  binary consistently, or users will get repeated access prompts on upgrade
  (`nexus/auth/store.py` explains the current issue).

## 9. Approximate effort

Planning estimates for engineers fluent in Rust and familiar with Nexus,
including tests and docs, **not verified** and not derived from a prototype.
Engineer-months.

| Work | Estimate |
| --- | ---: |
| Stage 0: schema extraction, conformance suite, goldens, baselines | 2–3 |
| Types, config, view/reducer, session storage | 2–3 |
| Model layer: transport, five HTTP adapters, registry, auth and sign-in | 3–4 |
| Loop and context (budget, compaction, caching) | 2–3 |
| Tools: permissions, bundles, builtins, shell jobs, patch, web tools | 3–4 |
| Agents and worktrees | 1.5–2 |
| Skills, hooks, MCP, extension host and its protocol | 3–4 |
| Runtime, host facade, daemon, supervisor, transports, presence, doctor, web serving | 3–4 |
| CLI and slash commands | 1 |
| Ratatui TUI to parity (companion report: 13–25 engineer-weeks) | 3–6 |
| Sidecars: Claude Agent, voice, AnyDoc | 1 |
| Devtools, mock mode, benchmark | 1 |
| Packaging, installers, update, CI matrix, signing | 1–2 |
| **Total** | **≈26–38 (≈2.2–3.2 engineer-years)** |

With two engineers, a calendar estimate is 13–19 months, assuming new features
are frozen or implemented twice. Every feature landed in Python during the
migration adds to the total.

## 10. Alternatives

| Alternative | Captures | Cost |
| --- | --- | --- |
| Do nothing; finish the shared daemon | Most of the memory cost | Already planned. |
| Ratatui TUI only ([ratatui-feasibility.md](../docs/ratatui-feasibility.md)) | TUI responsiveness | 3–6 engineer-months. |
| Rust extensions for measured hotspots (PyO3) | Specific CPU paths (grep scan, patch, reducer, JSON) | Weeks per hotspot; needs profiles first. |
| Single-file Python distribution (`uv` tool install, PyApp, PyInstaller-style bundling) | Most of the install-simplicity benefit | Weeks; not evaluated for Nexus's native dependencies. |
| Full Rust port (this report) | All of the above, plus a Python-free core | 2.2–3.2 engineer-years. |

The first four are cheaper and can be done in any order. Their measurements
show how much a full port would still add.

## 11. Decision gates and unresolved questions

**Proceed to stage 0** only if distribution simplicity, cold start, or
per-daemon memory is a product priority that the shared daemon and cheaper
packaging alternatives fail to deliver. Stage 0 is useful even if the port is
later abandoned: a protocol schema and conformance suite improve the Python
codebase too.

**Proceed past stage 1** only if replay equivalence holds on real sessions and
the team commits to Rust as the primary language for new harness work.

Before implementation, resolve:

- Which option in §5.1 for Python code extensions, and is breaking the
  `HostFacade` in-process API acceptable?
- Which platforms are supported? Is Windows in scope, and with which transport?
- Is Python acceptable as an optional sidecar runtime, or is "no Python" a hard
  requirement (which removes the Claude subscription provider and voice until
  native replacements exist)?
- Can feature development pause, or must every change land in both languages?
- Which workloads justify the port, and what measured baseline must Rust beat?

The architecture makes the port possible: clean contracts, a protocol boundary,
and an event-sourced log. The cost is the accumulated behavior and the Python
extension contract, not the language translation. Do not start a rewrite
without the conformance suite and golden outputs that would show it behaves
the same.
