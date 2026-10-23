# Module map

Every Python module under `nexus/`, one line each, grouped by package. Start from
the topic docs in the [index](README.md); use this page to find the file behind a
name. The descriptions come from module docstrings; edit them by hand when a
module's purpose changes. `tests/test_docs.py` fails when a module is missing
here, so add a row when you add a file.

## Repository layout

| Path | Contents |
| --- | --- |
| `nexus/` | the package (below) |
| `nexus/gateway/` | opt-in loopback device gateway; module file map below |
| `rust/desktop/` | GPUI desktop executable, native editor/transcript/panels, shared typed Settings renderer in `src/settings.rs`, declarative `src/keymap.rs`, GPT Live call bar and card in `src/live.rs`, bounded toast stack in `src/notice.rs`, turn minimap derivation in `src/minimap.rs`, code-mode detail cards in `src/code_detail.rs`, bounded opt-in CPU timing in `src/trace.rs`, and visual fixtures ([desktop.md](desktop.md)) |
| `rust/live/` | `nexus-live` helper for GPT Live calls: stdio JSONL (`src/protocol.rs`), call state machine (`src/app.rs`), webrtc-rs peer (`src/peer.rs`), Opus (`src/codec.rs`), rings/resampler/echo gate (`src/dsp.rs`), cpal or fake audio (`src/audio.rs`); protocol in `rust/live/README.md` |
| `skills/gpui-nexus/` | version-aware GPUI implementation/design guidance, reviewed upstream references and licenses |
| `skills/native-app-review/` | reusable native screenshot/review skill and local macOS bundling helper |
| `docs/` | this documentation |
| `tests/` | offline suite, Playwright/visual checks, fixtures, provider conformance ([testing.md](testing.md)) |
| `examples/` | facade example and extension samples ([devtools.md](devtools.md#examples-examples)) |
| `benchmark/` | live-model benchmark ([devtools.md](devtools.md#benchmark-benchmark)) |
| `websearch/` | local SearXNG Compose project |
| `plans/` | historical plan ledgers cited by docstrings ("plan section X.Y") |
| `scripts/hooks/` | `commit-msg` Conventional Commits check |
| `.github/workflows/` | `ci.yml`, `release.yml`, `install.yml`, `pr-title.yml` ([release.md](release.md)) |
| `install.sh`, `install.ps1` | installers |
| `pyproject.toml`, `uv.lock`, `release-please-config.json` | packaging, locked deps, release automation |
| `AGENTS.md` | rules and commands for agents; points here |
| `README.md`, `ARCHITECTURE.md`, `design.md`, `CHANGELOG.md` | user-facing and background (changelog is generated) |

## Non-Python package files

| Path | Contents |
| --- | --- |
| `nexus/agents/data/*.md` | packaged agent roles: `build`, `orchestrator` (roots); `advisor`, `task`, `quick` (subagents) |
| `nexus/model/data/models.min.json`, `NOTICE` | vendored models.dev snapshot and MIT attribution |
| `nexus/auth/NOTICE`, `nexus/voice/NOTICE`, `nexus/live/NOTICE` | third-party attributions |
| `nexus/live/prompts/instructions.md` | bundled GPT Live voice instructions (override: `<workspace>/.agents/live/instructions.md`) |
| `nexus/host_support/searchserver/` | packaged SearXNG `compose.yaml` and `searxng/settings.yml` |
| `nexus/ui/web/index.html`, `js/*.js`, `styles/*.css`, `assets/*` | the browser app ([web.md](web.md#file-map)) |

## Python modules

### `nexus/gateway/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Gateway package boundary |
| `app.py` | Gateway routes, authentication and bridge orchestration |
| `cli.py` | `python -m nexus gateway` command implementation |
| `httpd.py` | Loopback HTTP server and request handling |
| `policy.py` | Workspace and scope authorization policy |
| `runner.py` | Local daemon web-launch ticket integration |
| `sse.py` | Device event stream management |
| `store.py` | Pairing code and device credential registry |
| `upstream.py` | Host HTTP protocol bridge and origin validation |

### `nexus/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Public API: lazy re-exports only (import cost matters) |
| `__main__.py` | `python -m nexus` entry point (calls `cli.main`) |
| `cli.py` | The canonical terminal surface: argument parser and every subcommand ([cli.md](cli.md)) |
| `errors.py` | Exception taxonomy shared across Nexus |
| `events.py` | The public UI boundary: JSON-serializable events, no rendering or input |
| `runtime.py` | `Runtime`: the composition root that owns every manager ([architecture.md](architecture.md#runtime-runtimepy)) |
| `resources.py` | Fair process-wide process and provider resource admission |
| `worktree_turns.py` | Bounded saved-turn diffs and confirmed isolated revert commits. |
| `worktree_sessions.py` | Runtime-owned per-session worktrees, main-checkout checkpoints, per-turn commits, setup trust and promotion |
| `util.py` | Small dependency-free helpers shared by the lower layers |

### `nexus/agents/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Agents: restricted `*.md` subagent definitions |
| `last_choice.py` | `AgentChoiceStore`: last model and effort per root agent (`~/.nexus/agent_models.json`) |
| `manager.py` | AgentManager: deterministic subagent discovery, seeding, and tool selection |
| `manager_prompt.py` | Packaged constants of the workspace manager session: kind, the pinned `codex/gpt-6-luna` route, sign-in hint and system prompt; code, not a role file, so nothing user-supplied can extend it ([manager.md](manager.md)) |
| `model.py` | Subagent definitions: restricted `*.md` parsing and the immutable model |
| `runner.py` | SubagentRunner: bounded, nested execution of subagent definitions; `spawn` (always blocking) is `_prepare` then `_execute` |
| `pool.py` | AgentPool/RootAgents: session-scoped subagent tree budget with per-root-turn windows, active child registration, and legacy detached/persistent child lifecycle support |
| `claude_code_worker.py` | External Claude Code worker: one interactive PTY session per assignment running Claude's own tools under an explicit policy; hook + JSONL transcript readback, attention states, file-change verification |
| `antigravity_worker.py` | **NEW.** External Antigravity (`agy`) worker: one headless `agy --input-format stream-json --output-format stream-json` process per assignment running agy's own tools; stdin user event, NDJSON `init`/`step_update`/`result` readback, refuses tool/MCP/restriction fields and bypass modes; same duck contract as `claude_code_worker.py` |
| `handoff.py` | Failure handoff: bounded markdown report of a failed child's context, saved for the root to read |
| `worktree_integrate.py` | Apply an acknowledged, frozen worktree review to a clean parent checkout |
| `worktree_review.py` | Immutable, read-only review snapshots for finalized agent worktrees |
| `session_subagents.py` | Saves isolated child work and integrates its diff into its parent session. |
| `session_worktrees.py` | Authenticated service operations for session branches, checkout creation, commit, recreation and cleanup |
| `worktrees.py` | Safe Git worktree lifecycle management for subagents |

### `nexus/auth/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Local-only provider credentials: ChatGPT OAuth, GitHub Copilot, stored API keys |
| `api_key.py` | Pasted provider API keys kept in the private credential file (OpenCode Go) |
| `codex.py` | Experimental ChatGPT OAuth support for the private Codex Responses endpoint |
| `copilot.py` | GitHub.com device OAuth and short-lived GitHub Copilot credentials |
| `xai.py` | xAI SuperGrok device OAuth; refresh-token storage and Bearer headers for `cli-chat-proxy.grok.com` |
| `store.py` | Owner-only file-backed provider credentials, atomic writes and bounded locks |

### `nexus/client/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Transport-neutral clients that consume host protocol contracts |
| `protocol.py` | Pure, transport-neutral client for the host protocol |
| `turn_stream.py` | UI-neutral lifecycle for attaching to a turn before starting it |

### `nexus/config/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Layered configuration with v1 flat-config compatibility |
| `layers.py` | Layer loading, merging, and version detection |
| `paths.py` | Path resolution and containment |
| `schema.py` | msgspec structs for the v2 layered configuration |

### `nexus/context/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Context management |
| `budget.py` | Token budget accounting and the priority allocation algorithm |
| `cache.py` | Semantic token-count disk cache and generic prompt-cache boundaries |
| `compact.py` | Deterministic context compaction strategies |
| `handoff.py` | Versioned redacted deterministic briefs and active model-context derivation |
| `handoff_model.py` | Optional bounded foreground recall using the session's selected model |
| `counting.py` | Request-aware token counting with an exact-count disk cache |
| `manager.py` | Composable context assembly |
| `parts.py` | Composable context parts |

### `nexus/core/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Core layer: transport-independent plumbing the loop and managers build on |
| `bus.py` | Bounded async event fanout |
| `cancel.py` | Cooperative cancellation |
| `loop.py` | The agentic loop, written against protocols only |
| `registry.py` | Immutable, generation-stamped registries and an atomic reference |
| `turn.py` | Turn state machine, limits, usage, and outcome |
| `watch.py` | Dependency-free mtime_ns + size polling watcher |

### `nexus/devtools/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Developer tooling that only runs in dev mode |
| `debug_mcp_port.py` | `DaemonManagerPort`: the manager port over the host daemon protocol, for the debug MCP only |
| `manager_debug_mcp.py` | DEBUG-ONLY stdio MCP server exposing the manager's eight session tools (`session_show` always refused: no UI); enabled by its project `mcp.json` entry, no env gate |

### `nexus/devtools/mock/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Mock scenarios: a scripted provider, a scenario catalogue and a sandbox |
| `checks.py` | Reusable verdict checks |
| `directive.py` | The `⟦mock …⟧` actor directive |
| `dsl.py` | Scenario DSL |
| `mcp_server.py` | Dummy stdio MCP server (`tracker`, `docs`, `broken` profiles) wired only by the sandbox's generated `mcp.json` |
| `provider.py` | `MockProvider`: stateless, request-keyed replay |
| `runner.py` | Headless scenario runner |
| `sandbox.py` | Dev-mode sandbox workspace |

### `nexus/devtools/mock/scenarios/`

| File | Purpose |
| --- | --- |
| `__init__.py` | The mock scenario catalogue |
| `agent_limits.py` | Depth and fan-out limits: the runner must clamp or refuse, and tell the model |
| `bash_wait.py` | A slow, chatty command is yielded to the background and waited on once |
| `cancel.py` | Interactive: park mid-stream so `/cancel` (or Ctrl-C) can be exercised |
| `context_pressure.py` | Large tool outputs and climbing scripted usage to exercise budgets and /cost |
| `diff_review.py` | Edits that produce a real git diff in the sandbox repo |
| `errors.py` | Provider failures and tool failures: everything must surface, nothing may hang |
| `extensions.py` | A stdio MCP call, deferred MCP search and call, then a skill load, against the sandbox seeds |
| `hello.py` | Smallest run: one streamed markdown reply |
| `parallel_subagents.py` | Five `subagent` calls at once against the concurrency cap, mixed outcomes |
| `parallel_tools.py` | Single, batched, failing-member and write batches: parallel call states |
| `provider_failure.py` | Interactive: the provider fails once; the next message recovers |
| `question.py` | Interactive: the question tool, branching on the answers |
| `shell_persistent.py` | A persistent job (background, no timeout) is waited on with `until=match`, then stopped |
| `streaming_rich.py` | Rich markdown, wide lines, unicode and escape-looking text, with thinking |
| `stress.py` | Performance: every built-in tool, many subagents of every kind, nesting and failures |
| `tool_marathon.py` | A long chain of tool calls using most built-in tools, all inside the sandbox |

### `nexus/ext/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Hot extension management |
| `manager.py` | The serialized, atomic hot-reload orchestrator |
| `manifest.py` | The immutable extension manifest and its atomic, reference-counted handle |
| `quarantine.py` | Validate a hot-loaded extension file before it is staged and imported |
| `template.py` | The packaged workspace tool template |

### `nexus/hooks/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Lifecycle hooks |
| `manager.py` | The lifecycle hook manager |
| `model.py` | Hook contracts |

### `nexus/host/`

| File | Owns |
| --- | --- |
| `projects.py` | Bounded project registry, routing and scoped supervisor |

| File | Purpose |
| --- | --- |
| `__init__.py` | The host layer (L4½): the transport-neutral surface over one runtime |
| `daemon.py` | Daemon lifecycle, socket, handshake, and shutdown |
| `diagnostics.py` | Compatibility module path for the daemon diagnostics implementation |
| `doctor.py` | Compatibility exports for the bounded doctor projection |
| `facade.py` | The single surface API: `HostFacade` over one :class:`Runtime`; also opens the manager session (`ManagerOpen`/`ManagerState`) and injects its tool port |
| `presence.py` | Host-level presence: client attachment and first-responder permission leases |
| `protocol.py` | Transport-neutral wire contract for the host facade |
| `reports.py` | Legacy subagent report delivery and one-shot activity; historical detached-run reports are not replayed |
| `session_diagnostics.py` | Compatibility module path for the session diagnostics implementation |
| `supervisor.py` | Turn scheduling across sessions under one global concurrency cap |
| `web.py` | Browser-only HTTP routes and short-lived launch/session credentials |

### `nexus/host/transports/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Concrete host transports |
| `http_sse.py` | The HTTP/SSE surface transport |
| `uds.py` | The local Unix-socket transport: length-framed JSON for the CLI |

### `nexus/host_support/`

| File | Owns |
| --- | --- |
| `project_identity.py` | Canonical Git/plain project identity |

| File | Purpose |
| --- | --- |
| `attachments.py` | Prepare and validate image and document prompt attachments |
| `__init__.py` | Implementation helpers for host-facing read-only projections |
| `session_inspect.py` | Bounded durable session transcript and last-request inspection without opening handles |
| `session_handoff.py` | Read-only indexed active handoff brief projection |
| `agent_context.py` | The request context one subagent actually sent, shaped for the context header |
| `approval.py` | Bounded permission-request projection shared by attended host clients |
| `auto_title.py` | Starts, bounds and cancels the background session-title call for a new root session (never for the manager session) |
| `user_shell.py` | Composer `!` shell mode: runs the command with bash, records `shell.*` events, adds the output to context without a turn |
| `archive_protocol.py` | Wire records for the bounded session archive commands |
| `attachments.py` | Bounded attachment preparation (images stay image blocks, documents via the isolated AnyDoc worker); drafts expire |
| `browser_view.py` | Browser-safe reducer projection and compact structural JSON patches |
| `context_preview.py` | Privacy projection for a read-only, next-turn context preview |
| `doctor.py` | Bounded, redacted health projections for doctor |
| `git_head.py` | Subprocess-free, bounded read of the workspace's Git branch / detached HEAD / linked worktree (and its main checkout) for the Doctor `git` field and the sessions sidebar's worktree grouping |
| `git_diff.py` | Bounded, read-only Git diff projection for the host |
| `install.py` | Install, upgrade, and daemon-hygiene helpers |
| `uninstall.py` | `nexus uninstall`: what Nexus stored on this machine, safety checks, removal |
| `mock.py` | Host dispatch for the dev-mode `Mock*` commands |
| `model_settings.py` | Settings → Models and Session titles: tier lists, the subagent ceiling, the auto-title switch |
| `provider_auth.py` | Provider sign-in behind the host boundary: Settings → Providers |
| `provider_usage.py` | Plan usage and limits for every connected provider (`ProvidersUsage`) |
| `searchserver.py` | Start the loopback-only search service using packaged Compose assets |
| `session_archive.py` | Bounded host projections for durable session archive metadata |
| `session_jobs.py` | Host job commands (`SessionJobs`/`Log`/`Stop`/`Background`): session-partitioned, bounded reads of the runtime's shell `JobRegistry` |
| `errors.py` | `error_text(exc)`: the redacted, one-line, never-empty reason clients show in an error toast (a `TimeoutError()` names its type); used when another project's session fails to open |
| `session_preview.py` | Sessions dashboard text helpers: current-session message tails, the worked label and text cleaning (`message_tails`, `worked_label`, `clean_preview_text`); the dashboard preview projection was removed |
| `settings_inventory.py` | Bounded Settings console inventory, validation and safe file mutations |
| `settings_scope.py` | Single path policy for Settings console reads and mutations |
| `skills_install.py` | `SkillsInstall`: run pinned `npx skills add --copy` in a temp stage, validate each skill, import into the scope's `skills/` with conflict, trash and rollback handling |
| `setup.py` | First-run setup behind the host boundary |
| `usage_snapshot.py` | Periodic (`[models] usage_refresh_seconds`, default 300) and gate-triggered background refresh of the plan-usage snapshot (one fetch at a time, stale results dropped, fail-open, cancelled on shutdown) |
| `speech.py` | Isolated Paradee text-to-speech (onnxruntime, misaki) for the latest completed answer (`Speak`, `/speak`) |
| `socket_dir.py` | Private fallback directory for daemon sockets whose default path is too long |
| `update_check.py` | The "update available" notice (docs/release.md) |
| `manager_port.py` | The workspace-manager session's host side: `HostManagerPort` (visibility, provenance from the manager's own log, launch caps, `session.linked`, wait, cancel, send, show (`manager.show`), redaction, clipping), `FacadeManagerHost` (starts and cancels roots through `HostFacade`), `scan_manager_log` / `delegated_request` (Live provenance); injected via `Runtime.manager_port_factory` ([manager.md](manager.md)) |
| `live.py` | Host dispatch for GPT Live commands and the facade-backed `LiveHost` the call manager uses; a call on the manager session also needs the cross-session consent |
| `live_consent.py` | Once-per-machine GPT Live consent record (`<nexus home>/live-consent.json`), read and written by the `LiveConsent` command; separate `manager` scope for the cross-session notice |
| `voice.py` | Redacted host projection and dispatch for local voice commands |
| `workspace.py` | Bounded workspace path search respecting host read-deny boundaries |
| `workspace_browse.py` | Bounded, read-only `DirectoryList` (path-checked, symlinks listed but never followed, read-deny roots and hidden entries omitted), `FilePreview` (clipped, redacted text of one regular workspace file) and `GitStatus` (porcelain v2, deadline and byte cap) for the native sidebar's Files and Git tabs |
| `worktree_projection.py` | Allowlisted, bounded projection of owned-worktree records and diffs |

### `nexus/mcp/`

| File | Purpose |
| --- | --- |
| `__init__.py` | MCP integration |
| `bridge.py` | Bridge MCP descriptors and results into Nexus contracts |
| `client.py` | MCP client: three transports behind one normalized, upstream-free boundary |
| `errors.py` | Normalized MCP error taxonomy |
| `manager.py` | MCP server lifecycle manager |
| `search.py` | Pure bounded keyword ranking and exact MCP tool selection |

### `nexus/model/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Model layer: provider-neutral contracts shared by loop, managers, and adapters |
| `capabilities.py` | Capability descriptor |
| `http.py` | Shared HTTP transport and SSE framing for provider adapters |
| `message.py` | Provider-neutral message IR |
| `provider.py` | The provider protocol and the shared provider error taxonomy |
| `reasoning_effort.py` | Durable per-session reasoning-effort selection state |
| `effort_preferences.py` | Bounded machine-wide last reasoning effort per exact provider/model reference |
| `registry.py` | Model registry: a bounded, filtered, cache-backed view of models.dev |
| `request.py` | Structured model requests and sampling parameters |
| `router.py` | Resolve a configured model reference to a provider, model, and capabilities |
| `selection.py` | A validated per-session model selection (the `/model` override state) |
| `stream.py` | Normalized streaming events and the shared tool-call accumulator |
| `tiers.py` | Tier resolution for model references |
| `tokenizer.py` | Token accounting |
| `usage_gate.py` | Pure plan-usage gate: window kinds, thresholds, per-quota verdicts, route choice and the blocked-provider fallback filter |

### `nexus/model/providers/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Provider adapters behind the :class:`nexus.model.provider.Provider` protocol |
| `_claude_agent_worker.py` | Isolated official SDK call; private bounded JSON worker |
| `antigravity_cli.py` | **NEW.** Bounded local probes of the `agy` CLI (`--version`, `models`); presence only, sign-in is never verified, no token files read, no sign-out |
| `anthropic.py` | Anthropic Messages streaming adapter |
| `claude_agent.py` | Claude subscription provider through the official Agent SDK |
| `claude_agent_auth.py` | Bounded official CLI subscription status, usage and sign-in, never token files |
| `discovery.py` | File-loaded providers: `.agents/providers/*.py` |
| `gemini.py` | Google Gemini `generateContent` streaming adapter |
| `nvidia.py` | NVIDIA hosted-catalogue preset over the OpenAI Chat Completions adapter: endpoint, env key, attribution header |
| `ollama.py` | Ollama / llama.cpp local adapter |
| `openai.py` | OpenAI adapter: Responses API and Chat Completions behind one class |
| `opencode.py` | OpenCode adapter over its documented Agent Client Protocol surface |
| `scripted.py` | Deterministic, offline provider for tests and examples |
| `xai.py` | xAI (Grok) preset over the OpenAI Responses adapter: endpoint, `store: false`, cache header, image types |

### `nexus/net/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Network security primitives for outbound requests |
| `local_search.py` | Fixed-destination HTTP client for the local search service |
| `outbound.py` | Pinned, public-address-only HTTP transport for outbound service calls |

### `nexus/observability/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Bounded, privacy-reviewed observability projections shared by the host |
| `daemon.py` | Bounded, in-memory diagnostics for one daemon generation |
| `session.py` | Allowlisted, bounded projection of session lifecycle events for host logs |

### `nexus/session/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Session layer: shared SQLite state database, cross-process lock, handle |
| `agent_selection.py` | Durable descriptive root-agent selection for one session |
| `db.py` | Shared SQLite state, atomic child reservations, ownership queries and conservative evidence migration; lists and the stale-archive sweep exclude the `manager` kind |
| `child_ids.py` | Stable legacy-compatible logical-to-stored child id mapping; no ownership inference |
| `handoff.py` | Atomic same-session summary, head, marker and continuation admission transaction |
| `tree.py` | Bounded read-only, redacted ownership tree and OS-lock liveness projection |
| `export.py` | Structured, transport-neutral SQLite session export |
| `ids.py` | Session ID validation shared by SQLite session rows and lock paths |
| `root.py` | Immutable validated session execution-root value and durable relative event conversion |
| `lock.py` | Cross-process session locking |
| `context_authz.py` | Pure ancestry authorisation for context reads: owner-edge walk with `context_read = 1` on every hop, same project, depth cap, trash denies |
| `context_port.py` | `ContextReadPort`: bounded, redacted, untrusted-marked reads (meta, messages, tool calls, search, report) over an authorised target |
| `names.py` | Immutable session names: v2 vocabulary, CSPRNG allocation with retry then suffix, validation, backfill batches |
| `manager.py` | SessionManager: open/fork/replay/list/archive/delete/export; `kind_of`, `manager_summary`, and the manager session's archive/delete/fork refusal |
| `records.py` | Versioned records persisted by the SQLite session store |
| `session.py` | The public session handle over SQLite records; `Session.kind`, and no hooks for a `manager` session |
| `root.py` | Immutable per-session working root and bounded relative event serialization |
| `title.py` | Automatic session titles: the small side call to a cheap model, the title prompt and reply cleaning |
| `snapshot.py` | Versioned, derived snapshots for fast session resume |
| `submissions.py` | Request-id validation and canonical admission digests; no scheduling or effect replay |

### `nexus/skills/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Skills: restricted `SKILL.md` discovery with progressive disclosure |
| `activation.py` | Session/turn-local :class:`SkillActivation` overlay |
| `errors.py` | Skill-layer exception taxonomy |
| `frontmatter.py` | Restricted, dependency-free `SKILL.md` frontmatter parsing |
| `invocation.py` | Delimited, bounded skill invocation output |
| `manager.py` | SkillManager: deterministic discovery and progressive disclosure |
| `model.py` | Immutable skill metadata |
| `resources.py` | Bundled skill resource resolution and inventory |

### `nexus/tools/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Tool contracts, bundles, permissions, and the manager |
| `bundles.py` | Built-in bundle and profile definitions |
| `loader.py` | Hot-load `.py` tool modules under version-stamped, generation-unique names |
| `manager.py` | ToolManager: selected registry, validation, scheduling, and dispatch |
| `manager_port.py` | `ManagerPort` protocol (`find` `status` `result` `launch` `wait` `cancel` `send` `show`) and bounded value types for the workspace manager session ([manager.md](manager.md)) |
| `manager_tools.py` | The eight manager tools over an injected `ManagerPort` and `build_manager_catalog`, the manager's whole catalog ([manager.md](manager.md)) |
| `names.py` | Exact, one-way compatibility mapping for public tool names (`bash`/`Bash` → `shell`, legacy shell arguments, wildcard-rule aliases) |
| `permissions.py` | Permission grammar, evaluation, path security, and approval primitives |
| `questions.py` | Session-scoped question requests for tools and agents |
| `builtin/worktree.py` | Dynamic status, commit, and promotion tools bound to per-session callbacks |
| `shell_syntax.py` | Conservative detection and splitting of unquoted shell control syntax for glob permission rules |
| `spec.py` | Tool contracts |

### `nexus/tools/codemode/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Optional code-mode package ([tools.md](tools.md#code-mode-the-code-tool)) |
| `catalog.py` | Callable catalog from the frozen tool set, exposure filter, `render_catalog` within the inline budget, skipped names |
| `context_ns.py` | Flat `context_*` and `llm` host functions over `ContextReadPort`, with per-program sub-call limits |
| `ipc.py` | Bounded, framed JSON IPC between daemon and helper (finite numbers, 32-level nesting) |
| `monty_adapter.py` | Isolated helper process; sole importer of `pydantic_monty` |
| `runner.py` | Daemon-side runner: helper lifecycle, admission, id-correlated callbacks, `killpg` |
| `store.py` | Bounded JSON `store`/`load` committed on success, replayed from `code.store` |
| `stubs.py` | Python stubs from JSON Schema (`Literal`, `| None = None`) for type checking |
| `tool.py` | The `code` `RegisteredTool`, result text, events |

### `nexus/tools/builtin/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Registration surface for the built-in tools (`RegisteredTool` pairs per bundle) |
| `_anydoc_client.py` | Bounded subprocess client for the private AnyDoc conversion worker |
| `_anydoc_worker.py` | Bounded, offline AnyDoc document-to-Markdown worker |
| `_grep_scan.py` | Isolated, killable regex-scan worker for the `Grep` tool |
| `_html_markdown.py` | Bounded, offline conversion of untrusted HTML into Markdown |
| `_job_recovery.py` | Restart recovery (P7): per-registry owner lock + record, process identity via `ps`, the verified orphan sweep, and the live-owner probe |
| `_jobs.py` | Shell job registry and process-group lifecycle management |
| `_output_limit.py` | Shell output limit (2,000 lines / 50 KiB) shared by `shell` and `!` mode; spills the full text to a private temp file unless the caller names a job log |
| `_patch_commit.py` | Guarded commit and rollback for immutable staged patch changes |
| `_patch_parse.py` | Pure, bounded parser for the Nexus multi-file patch format |
| `_patch_stage.py` | Pure snapshot-based planning for parsed Nexus patches |
| `_shell_logs.py` | Per-job combined shell logs (head/tail buffers, disk bounds, retention) and the persistent session cwd record |
| `apply_patch.py` | Built-in `apply_patch`: validate and apply bounded multi-file patches |
| `bash.py` | Compatibility shim: the old `bash` import path for the `shell` tool (translates legacy arguments) |
| `bash_output.py` | `BashOutput`: read buffered output and status for a registry-owned job |
| `edit.py` | Built-in `Edit`: exact, single-commit string replacement |
| `glob.py` | Built-in `Glob`: deterministic, workspace-rooted path matching |
| `grep.py` | Built-in `Grep`: deterministic, text-only recursive content search |
| `kill_shell.py` | `KillShell`: terminate a registry-owned background shell job |
| `ls.py` | Built-in `LS`: deterministic, bounded directory listing |
| `meta.py` | The meta builtins: reload, inspect, and author extensions |
| `multiedit.py` | Built-in `MultiEdit`: validate every edit, then commit once atomically |
| `question.py` | `question`: ask the operator one question and wait for the answer |
| `escalate.py` | `escalate` (subagents only): report a block to the parent, which resolves it or asks the user |
| `read.py` | Built-in `Read` and the private helpers shared by the fs tools |
| `shell.py` | `shell` (was `bash`): run commands, control jobs, read job logs (`log`) and set the session cwd (`cd`) |
| `skill.py` | `Skill`: progressive disclosure of a skill body or bundled resource |
| `task.py` | `subagent` (legacy name `Task`): spawn a bounded subagent (bundle `task`) |
| `worktree.py` | Dynamic status, commit, and promotion tools bound to per-session callbacks |
| `webfetch.py` | Fetch a public web page and return bounded, explicitly untrusted Markdown |
| `websearch.py` | Search configured SearXNG instances and return bounded untrusted results |
| `write.py` | Built-in `Write`: atomic, symlink-safe file creation and replacement |

### `nexus/ui/`

| File | Purpose |
| --- | --- |
| `__init__.py` | UI adapters |
| `jsonl.py` | JSONL passthrough: every event envelope, unattended |
| `turn_stream.py` | Backward-compatible import path for host turn subscription sequencing |

### `nexus/ui/cli/`

| File | Purpose |
| --- | --- |
| `__init__.py` | The host-protocol client and one-shot renderer used by terminal surfaces |
| `approve.py` | Terminal-side approval |
| `client.py` | Backward-compatible import path for the transport-neutral host client |
| `commands.py` | Slash commands as data |
| `details.py` | Pure, deterministic details of a reduced conversation view |
| `render.py` | Plain one-shot/JSONL human rendering of the event stream |
| `run.py` | One-shot runs (`nexus run "..."` over the daemon) |
| `live.py` | `nexus live`: a headless GPT Live call (consent prompt, `LiveCall` helper, labelled captions, delegations and appends; hang-up on Ctrl+C or end of input) ([live.md](live.md#headless-cli-nexus-live)) |
| `stream.py` | Turn streaming helpers shared by the one-shot and interactive surfaces |
| `uds.py` | Unix-socket transport for the CLI, over the canonical host wire |

### `nexus/ui/web/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Static browser client for the Nexus daemon |
| `js/agent-settings.js` | Guided agent routing editor (session/model/tier, ordered fallbacks) that preserves prompt and unrelated frontmatter and checks SHA conflicts |
| `js/composer-navigation.js` | Composer history (Up/Down, per-session, draft restore) and Ctrl+A/E/K/U/W line editing |
| `js/context-preferences.js` | Browser-local visibility of context-header cards; hidden cards stay in the full request context |
| `js/mcp-settings.js` | Settings pane for MCP servers: scope, loading mode, enabled state and errors via the host contract |
| `js/model-settings.js` | Settings pane for default model, title model and tier routes |
| `js/speech.js` | `/speak`, speech status and stop on the daemon host, with explicit model download confirmation |
| `js/markdown.js` | Shared HTML-free transcript and context Markdown renderer; safe links, aligned tables, nested and task lists, quotes, and code fences with a language label and Copy button |
| `js/highlight.js` | Bounded, presentation-only syntax colouring for fenced code (comments, strings, numbers, keywords, calls; diff lines) |
| `js/fuzzy.js` | Fuzzy matcher for the palette and model picker (port of `ui_support/fuzzy.py`) |
| `js/voice-strip.js` | Floating dictation waveform and bounded live transcript preview |

### `nexus/ui_support/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Pure presentation helpers shared by terminal surfaces |
| `agent_frontmatter.py` | Read and rewrite the simple `key: value` frontmatter of an agent `*.md` |
| `clipboard.py` | Bounded local system clipboard image reading for terminal attachment uploads |
| `completion.py` | Toolkit-free composer completion shared by the native shell |
| `context.py` | Pure display projections for context preview and session usage |
| `context_header.py` | Toolkit-free context header blocks, complete inventories and bounded fallback previews; skill tokens estimate the included index rather than the available catalogue |
| `details.py` | Toolkit-free details sidebar data (session rows, modified files, MCP rows) shared by both shells, plus the terminal right sidebar's stored tabs (`Changes` = `Files`) and width bounds (mirrored in `rust/tui/src/details_sidebar.rs`) |
| `external_worker.py` | Labelled authority/outcome blocks for an external Claude Code worker (`ConversationView.external`): launch authority, attention state with the manual instruction, transcript path, usage unknown |
| `fuzzy.py` | Shared fuzzy matcher (score and match positions) for the command palette and model picker |
| `hints.py` | Randomized tips shown in the middle of an empty session (mirrored in `js/hints.js`) |
| `manager.py` | Toolkit-free presentation of the workspace manager session from `ManagerState`: pinned sidebar entry, cross-session Live notice, and refused chat commands; mirrors (never imports) the facade and prompt constants ([manager.md](manager.md)) |
| `mock_args.py` | Shared `/mock` argument handling for the chat surfaces |
| `mock_cli.py` | `nexus mock list\|run\|clean` (dev mode only) |
| `model_choice.py` | Toolkit-free model picker sort, fuzzy rank, grouping and effort rules |
| `native_images.py` | Bounded host-backed draft and submitted image previews for the desktop; no path or remote URL reads |
| `native_schedule.py` | `UpdateCoalescer`: frame-rate coalescing of native bridge updates; user actions flush immediately |
| `prompt_history.py` | Durable prompt history for the native composer: 500 entries, prompts up to 1M characters, 16 MiB file budget (oldest dropped first) |
| `prompts.py` | UI-neutral choices for operator prompts: approvals and agent questions |
| `session_controller.py` | Shared host-client lifecycle, selection and canonical reducer seam |
| `session_groups.py` | Shared project and local-date grouping for terminal session lists |
| `session_status.py` | Shared session-card status words, relative age and sub-line |
| `settings_help.py` | One-line help per Settings area, shared by both consoles |
| `settings_page.py` | The typed one-page Settings model (blocks, controls, operations); control-safe and bounded |
| `shortcuts.py` | Shared terminal shortcut and leader reference |
| `sidebar.py` | Toolkit-free data of the native left sidebar: tab/width bounds (mirrored in `rust/tui/src/sidebar.rs`), manager-session filter, Projects grouping of repositories and worktrees, and control-safe Files and Git payloads from host results |
| `speech_download.py` | Rules and wording for the `/speak` model download, shared by both clients |
| `speech_settings.py` | Host-backed `/speak` settings (single Paradee voice) with safe `[speech]` TOML updates |
| `text.py` | Control-safe, credential-redacted text for terminal presentation |
| `tier_settings.py` | Rows, labels and help text for Settings → Models, Session titles and an agent's Tiers row |
| `timeline.py` | Pure formatting and filtering for reducer-backed conversation timelines |
| `tool_details.py` | Presentable tool call details: every parameter and output, none of the JSON |
| `tool_signal.py` | The relevant-only view of a tool call: what it did and what it produced |
| `usage.py` | Provider usage formatting shared by surfaces (`ProvidersUsageResult`) |
| `live_card.py` | Rows of the GPT Live timeline card from a `LiveCallView`: captions, "Sent to agent", "Told voice", started/ended rows, commentary collapsed with a count, clips announced; shared by native clients |
| `live_helper.py` | `nexus-live` process wrapper (binary lookup, bounded JSONL, offer/answer/mute/stop, helper-event forwarding) and `LiveCall`, the client half of a GPT Live call; stdlib only, shared by the terminal, desktop and CLI clients |
| `voice_capture.py` | Bounded 16 kHz microphone capture for TUI dictation |
| `voice_settings.py` | Host-backed voice configuration shared by terminal surfaces |
| `live_settings.py` | Host-backed GPT Live `[live]` settings (mirrored bounds, voices, pinned model) with safe TOML updates and reset |

### `nexus/view/`

| File | Purpose |
| --- | --- |
| `__init__.py` | The pure view layer |
| `fold.py` | Delta accumulation, dedup, and log folding for the pure view reducer |
| `model.py` | Renderable view-model types for the pure event reducer |
| `reduce.py` | The pure, synchronous event reducer |

### `nexus/live/`

| File | Purpose |
| --- | --- |
| `__init__.py` | GPT Live voice calls: a daemon-side manager (docs/live.md) |
| `protocol.py` | Wire shapes of a call: signaling body, inbound event union with `Unknown` fallback, outbound appends, 500-byte UTF-8 chunking |
| `bridge.py` | Delegation bridge: voice requests become queued or steering user messages; session events become progress, final and notice appends |
| `manager.py` | One call per daemon: state machine, deadline, detach watchdog, exactly one `live.ended`, durable `live.*` events |
| `transport.py` | `LiveTransport` seam and `CodexLiveTransport`: signaling POST, sideband WebSocket with bounded reconnect, redacted errors |

### `nexus/voice/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Local-only voice inference with bounded, transient audio processing |
| `audio.py` | Strict bounded PCM WAV handling for voice input |
| `bark.py` | Strict, bounded reader for `.bark` weight files (untrusted data; lazy per-tensor decode) |
| `engine.py` | Local Bark adapter: `.bark` weights into a stock Transformers `ParakeetForTDT` |
| `manager.py` | Serialized voice lifecycle and inference |
| `model.py` | Voice lifecycle and transcription values |
| `store.py` | Pinned, bounded local storage for the Bark weights and NVIDIA processor files |


### `nexus/ui/desktop/`

| File | Contract |
| --- | --- |
| `run.py` | GPUI executable discovery and host-only native presentation bridge launch. |
| `wire.py` | Topic-fingerprinted incremental desktop snapshot serialization and transcript block deltas. |
| `wire_schedule.py` | Import shim re-exporting the shared `UpdateCoalescer` for the desktop bridge. |

### `nexus/ui/ratatui/`

| Module | Responsibility |
| --- | --- |
| `__init__.py` | Experimental native surface package |
| `prototype.py` | Host adapter and labelled snapshot projection |
| `bridge.py` | Sessions dashboard projection and list polling |
| `manager.py` | Native Manager entry: reads `ManagerState` for the pinned row, `ManagerOpen`, and the manager's Live route through the ordinary Live bridge, and `on_event`, which acts on a fresh `manager.show` for this client; host-side rules stay authoritative ([manager.md](manager.md)) |
| `permission_prompt.py` | Structured permission prompt fields for the native client: `title`, `since`, `what`, `why` |
| `verb_groups.py` | Verb groups: the quiet one-line summary of a run of tool calls (thoughts are members but not counted); change tools group apart from other tools; compact labels above 5 members; `Working` and the newest-5 member window while running |
| `context_sections.py` | Skills and MCP context sections: multi-line cards, the skill page and the MCP server page |
| `wire.py` | Terminal schema-3 section deltas and ordered transcript suffixes |
| `sidebar.py` | Native left sidebar controller: open/tab/width state, host-backed Projects, Files and Git loads with stale-result guards, validated client actions, the `sidebar` snapshot block; its Sessions tab reuses `bridge.py` |
| `stream_projection.py` | Changed-tail projection for ordinary root text/thought deltas |
| `turn_status.py` | Running-turn status line fields (activity, since, tokens) for the native client |
| `transcript_data.py` | Bounded immutable-tool presentation cache |
| `background.py` | Bounded independent completion/model/clipboard tasks with stale guards |
| `jobs.py` | Jobs tab of the native details sidebar: the session's shell jobs and one bounded log tail through the existing host job commands; polls only while drawn, drops stale replies |
| `trace.py` | Bounded, body-free Python bridge timings and exit report |

The Rust client lives in `rust/tui/src/`: `main.rs` (terminal loop, key and mouse
handling), `input.rs` (action writers, editor keys, picking, OSC 52 base64),
`bridge.rs` (the versioned snapshot contract), `editor.rs` (grapheme editor),
`render.rs` (palette, layout regions, the draw pass), `render/toasts.rs` (toasts: ingest by id, timers paused on hover, hit targets, drawn by the `rust/widgets` kit), `render/notifications.rs` (client-side toast history of 50, the top-bar counter text and the notifications drawer), `panic_log.rs` (panic hook and bounded `~/.nexus/tui-panic.log` crash log), `splash.rs` (launch splash state machine: converging wordmark, milestone bar, flash, curtain), `render/components.rs`
(shared button/toggle/section/selectable styles and bounded local hover state;
composer controls are the first adopters), `render/chrome.rs` (top bar,
tabs, sessions and details sidebars), `render/context.rs` (reusable width-aware inventory sections: tools 1–5 columns, skills 1–2 columns, MCP one column, all capped at five rows with total/omitted indicators; unchanged host detail operations; the closed-header `total` row), `render/dialogs.rs` (dialog frames, toned
panel text, Settings area list, prompt and logs regions, completion popup),
`settings_page/` (one-page Settings: `model.rs` parses the host's typed page, `input.rs` focus/keys/mouse, `draw.rs` lays the blocks out and `grok.rs` draws them in the Grok row grammar, `root.rs` is the Grok-style root list with search and drill rows), `transcript.rs` (blocks to rows, diffs) and `markdown.rs`. `rust/widgets` is the shared component kit (see its crate docs). `main()` is still one long
loop over local state; splitting it further needs a state struct and is not done.
| `actions.py` | Native shell slash dispatch, attachments and host-backed panels |
| `controller.py` | Continuous native subscription using canonical bootstrap and reduction |
| `preferences.py` | Native shell preferences |
| `ownership.py` | Shared read-only ownership tree, facts, transcript and request projection for native clients |
| `workflows.py` | Settings, provider, context, session and worktree workflows |
| `speak_pages.py` | `/speak` consent, download progress and speaking in the native client |
| `tier_pages.py` | An agent's Tiers row (Settings → Models and titles are on the Models page) |
| `settings_pages/__init__.py` | Registry of the one-page Settings areas (`PAGE_AREAS`) and their lazy module lookup |
| `settings_pages/models.py` | Settings → Models: default chain, session titles, tier tabs, subagent limit, catalogue |
| `settings_pages/providers.py` | Settings → Providers: one section per provider with in-place sign-in, API key and sign-out |
| `settings_pages/voice.py` | Settings → Voice & speech: local dictation and Paradee speech with consent-gated downloads |
| `settings_pages/live.py` | Settings → GPT Live: voice, limits, progress, instructions path, raw log and reset for `[live]` |
| `settings_pages/appearance.py` | Settings → Appearance (theme) |
| `settings_pages/layout.py` | Settings → Layout (sidebars and context header) |
| `settings_pages/keys.py` | Settings → Keyboard: every shortcut, read-only, from the shared table |
| `settings_pages/files.py` | Shared builder for file-backed areas: scope control, Edit/New file/Reset rows |
| `settings_pages/agents.py` | Settings → Agents: default root agent and agent files (always global) |
| `settings_pages/tools.py` | Settings → Tools: tool files, global or per project |
| `settings_pages/skills.py` | Settings → Skills: SKILL.md files, global or per project |
| `settings_pages/mcp.py` | Settings → MCP servers: per-server switch and tool loading, plus mcp.json files |
| `logs.py` | Bounded paged native diagnostics with routine-entry folding |
| `voice.py` | Bounded native dictation using shared capture and host transcription |
| `focus.py` | Reports foreground session changes through SessionFocus, including initial attachment and reconnects |
| `manager_follow.py` | Independent manager event subscription for Live captions and cross-session approval/question attention |
| `live.py` | Native GPT Live call bridge: `/live` commands, consent dialog, helper lifecycle through `ui_support/live_helper.py`, call-bar state, error panels |
| `desktop.py` | Explicit clipboard operations with byte/time bounds |
| `run.py` | Native launch seam and binary discovery |


| Native module | Contract |
| --- | --- |
| `rust/tui/src/copy_button.rs` | Hover-revealed Copy buttons on user cards and code fences: row markers, hit test, text recovery |
| `rust/tui/src/disclosure.rs` | Rust-local transcript disclosure, session/page LRU choices and borrowed presentation |
| `rust/tui/src/graphics.rs` | One-shot `/dev/tty` probe choosing the `ratatui-image` protocol (Kitty, Sixel, iTerm2, half blocks) and cell size; `NEXUS_IMAGE_PROTOCOL` override |
| `rust/tui/src/live_anim.rs` | GPT Live panel open/close motion: height 0 to full in 260 ms (ease-out cubic), 140 ms close from a ghost of the last call state, header accent sweep, frame request only while moving, jump to final state with animations off |
| `rust/tui/src/live_wave.rs` | GPT Live motion band: block-height bars per state (breathing/mic-driven listening, accent gradient speaking, shimmer connecting/working, flat muted/ended), 30 Hz pure function of the frame, static and ASCII fallbacks, `wants_frames` |
| `rust/tui/src/live_bar.rs` | GPT Live pane header and captions: caps phase + its motion band (`live_wave.rs`) + clock, `Mic` history and eased levels, chip/word-by-word caption rows |
| `rust/tui/src/local_ui.rs` | Optimistic sidebar/tab/file/log toggles with ordered acknowledgements |
| `rust/tui/src/trace.rs` | Bounded opt-in native timing samples, percentile summaries and exit report |
| `rust/tui/src/config.rs` | Optional local UI appearance preferences (`tui.json`), including `animate_activity` |
| `rust/tui/src/mermaid.rs` | Self-contained Mermaid renderer (flowchart, sequence, state) as Unicode art; unsupported or oversized diagrams return `None` for the code-block fallback. Registered and called from `markdown.rs`; behaviour is unverified end to end |
| `rust/tui/src/picker.rs` | Sessions dashboard state, filtering and keys; effects returned as operations |
| `rust/tui/src/session.rs` | Stable prompt anchoring and permanent transcript tail space |
| `rust/tui/src/details_sidebar.rs` | Right details sidebar: stored width (24..=60, default 40) and room for the conversation, drawer below 100 columns, wrapped Session/Changes/MCP/Logs tabs, per-tab rows, pure `geometry` shared by painting and hit-testing (tabs, Logs copy-ID line, Changes rows), divider hover and drag |
| `rust/tui/src/sidebar.rs` | Left sidebar: bounded width and room for the conversation, wrapping tab cells shared by painting and hit-testing, Projects/Sessions/Files/Git tabs, divider drag, focus and key routing; the Sessions tab drives `picker::Dashboard` |
| `rust/tui/src/render/sessions.rs` | Compact centered, bordered Sessions page (max 72 columns, content height): time groups, one line per session (status glyph, name, last-used age), header actions |
| `rust/tui/src/transcript/groups.rs` | Verb-group header (`▾ Working` while running), the running grey member tree and expanded member boxes |
| `rust/tui/src/transcript/fold.rs` | Fold transient of a finished tool group: rows leave one per frame, then the header flash (pure over an `Instant`) |
| `rust/tui/src/transcript/header.rs` | Context header rows, grey tones, open form and chip |
| `rust/tui/src/transcript/look.rs` | Shared look of activity rows: glyphs, time format, live-run marker, animation recolouring and the streaming write head |
| `rust/tui/src/transcript/signal.rs` | Typed rows of a tool's relevant-only view (`[kind, text]` pairs) |
| `rust/tui/src/transcript/thought.rs` | Reasoning rows: running `Thinking…` tail and finished `Thought for N` |
| `rust/tui/src/transcript/tools.rs` | Standalone tool rows and the shared expanded body |

The manual `tests/ratatui_performance_check.py` script measures controlling-PTY
streaming, input and CPU cost.

- `nexus/tools/codemode/`: code-mode (on by default) catalog/stubs, JSON event store,
  bounded IPC and daemon-side runner. `monty_adapter.py` is the isolated helper
  and sole importer of `pydantic_monty`; `runner.ContextNamespaceProvider` defines
  the context integration seam (`bind(session, targets) -> dict` of callables).
