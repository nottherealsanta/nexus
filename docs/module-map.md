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
| `nexus/auth/NOTICE`, `nexus/voice/NOTICE` | third-party attributions |
| `nexus/host_support/searchserver/` | packaged SearXNG `compose.yaml` and `searxng/settings.yml` |
| `nexus/ui/tui/app.tcss` | all Textual CSS |
| `nexus/ui/web/index.html`, `js/*.js`, `styles/*.css`, `assets/*` | the browser app ([web.md](web.md#file-map)) |

## Python modules

### `nexus/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Public API: lazy re-exports only (import cost matters) |
| `__main__.py` | `python -m nexus` entry point (calls `cli.main`) |
| `cli.py` | The canonical terminal surface: argument parser and every subcommand ([cli.md](cli.md)) |
| `errors.py` | Exception taxonomy shared across Nexus |
| `events.py` | The public UI boundary: JSON-serializable events, no rendering or input |
| `runtime.py` | `Runtime`: the composition root that owns every manager ([architecture.md](architecture.md#runtime-runtimepy)) |
| `util.py` | Small dependency-free helpers shared by the lower layers |

### `nexus/agents/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Agents: restricted `*.md` subagent definitions |
| `last_choice.py` | `AgentChoiceStore`: last model and effort per root agent (`~/.nexus/agent_models.json`) |
| `manager.py` | AgentManager: deterministic subagent discovery, seeding, and tool selection |
| `model.py` | Subagent definitions: restricted `*.md` parsing and the immutable model |
| `runner.py` | SubagentRunner: bounded, nested execution of subagent definitions |
| `handoff.py` | Failure handoff: bounded markdown report of a failed child's context, saved for the root to read |
| `worktree_integrate.py` | Apply an acknowledged, frozen worktree review to a clean parent checkout |
| `worktree_review.py` | Immutable, read-only review snapshots for finalized agent worktrees |
| `worktrees.py` | Safe Git worktree lifecycle management for subagents |

### `nexus/auth/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Local-only provider credentials: ChatGPT OAuth, GitHub Copilot, stored API keys |
| `api_key.py` | Pasted provider API keys kept in the private credential file (OpenCode Go) |
| `codex.py` | Experimental ChatGPT OAuth support for the private Codex Responses endpoint |
| `copilot.py` | GitHub.com device OAuth and short-lived GitHub Copilot credentials |
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

### `nexus/devtools/mock/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Mock scenarios: a scripted provider, a scenario catalogue and a sandbox |
| `checks.py` | Reusable verdict checks |
| `directive.py` | The `⟦mock …⟧` actor directive |
| `dsl.py` | Scenario DSL |
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
| `hello.py` | Smallest run: one streamed markdown reply |
| `parallel_subagents.py` | Five `subagent` calls at once against the concurrency cap, mixed outcomes |
| `parallel_tools.py` | Single, batched, failing-member and write batches: the TUI/web gutter states (⎾ │ ⎿) |
| `provider_failure.py` | Interactive: the provider fails once; the next message recovers |
| `question.py` | Interactive: the question tool, branching on the answers |
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

| File | Purpose |
| --- | --- |
| `__init__.py` | The host layer (L4½): the transport-neutral surface over one runtime |
| `daemon.py` | Daemon lifecycle, socket, handshake, and shutdown |
| `diagnostics.py` | Compatibility module path for the daemon diagnostics implementation |
| `doctor.py` | Compatibility exports for the bounded doctor projection |
| `facade.py` | The single surface API: `HostFacade` over one :class:`Runtime` |
| `presence.py` | Host-level presence: client attachment and first-responder permission leases |
| `protocol.py` | Transport-neutral wire contract for the host facade |
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

| File | Purpose |
| --- | --- |
| `attachments.py` | Prepare and validate image and document prompt attachments |
| `__init__.py` | Implementation helpers for host-facing read-only projections |
| `agent_context.py` | The request context one subagent actually sent, shaped for the context header |
| `approval.py` | Bounded permission-request projection shared by attended host clients |
| `archive_protocol.py` | Wire records for the bounded session archive commands |
| `attachments.py` | Bounded attachment preparation (images stay image blocks, documents via the isolated AnyDoc worker); drafts expire |
| `browser_view.py` | Browser-safe reducer projection and compact structural JSON patches |
| `context_preview.py` | Privacy projection for a read-only, next-turn context preview |
| `doctor.py` | Bounded, redacted health projections for doctor |
| `git_head.py` | Subprocess-free, bounded read of the workspace's Git branch / detached HEAD / linked worktree for the Doctor `git` field |
| `git_diff.py` | Bounded, read-only Git diff projection for the host |
| `install.py` | Install, upgrade, and daemon-hygiene helpers |
| `mock.py` | Host dispatch for the dev-mode `Mock*` commands |
| `provider_auth.py` | Provider sign-in behind the host boundary: Settings → Providers |
| `provider_usage.py` | Plan usage and limits for every connected provider (`ProvidersUsage`) |
| `searchserver.py` | Start the loopback-only search service using packaged Compose assets |
| `session_archive.py` | Bounded host projections for durable session archive metadata |
| `settings_inventory.py` | Bounded Settings console inventory, validation and safe file mutations |
| `settings_scope.py` | Single path policy for Settings console reads and mutations |
| `setup.py` | First-run setup behind the host boundary |
| `socket_dir.py` | Private fallback directory for daemon sockets whose default path is too long |
| `update_check.py` | The "update available" notice (docs/release.md) |
| `voice.py` | Redacted host projection and dispatch for local voice commands |
| `workspace.py` | Bounded workspace path search respecting host read-deny boundaries |
| `worktree_projection.py` | Allowlisted, bounded projection of owned-worktree records and diffs |

### `nexus/mcp/`

| File | Purpose |
| --- | --- |
| `__init__.py` | MCP integration |
| `bridge.py` | Bridge MCP descriptors and results into Nexus contracts |
| `client.py` | MCP client: three transports behind one normalized, upstream-free boundary |
| `errors.py` | Normalized MCP error taxonomy |
| `manager.py` | MCP server lifecycle manager |

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

### `nexus/model/providers/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Provider adapters behind the :class:`nexus.model.provider.Provider` protocol |
| `_claude_agent_worker.py` | Isolated official SDK call; private bounded JSON worker |
| `anthropic.py` | Anthropic Messages streaming adapter |
| `claude_agent.py` | Claude subscription provider through the official Agent SDK |
| `claude_agent_auth.py` | Bounded official CLI subscription status, usage and sign-in, never token files |
| `discovery.py` | File-loaded providers: `.agents/providers/*.py` |
| `gemini.py` | Google Gemini `generateContent` streaming adapter |
| `ollama.py` | Ollama / llama.cpp local adapter |
| `openai.py` | OpenAI adapter: Responses API and Chat Completions behind one class |
| `opencode.py` | OpenCode adapter over its documented Agent Client Protocol surface |
| `scripted.py` | Deterministic, offline provider for tests and examples |

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
| `db.py` | The shared SQLite state database: one file for every project |
| `export.py` | Structured, transport-neutral SQLite session export |
| `ids.py` | Session ID validation shared by SQLite session rows and lock paths |
| `lock.py` | Cross-process session locking |
| `manager.py` | SessionManager: open/fork/replay/list/archive/delete/export |
| `records.py` | Versioned records persisted by the SQLite session store |
| `session.py` | The public session handle over SQLite records |
| `snapshot.py` | Versioned, derived snapshots for fast session resume |

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
| `names.py` | Exact, one-way compatibility mapping for public tool names |
| `permissions.py` | Permission grammar, evaluation, path security, and approval primitives |
| `questions.py` | Session-scoped question requests for tools and agents |
| `spec.py` | Tool contracts |

### `nexus/tools/builtin/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Registration surface for the built-in tools (`RegisteredTool` pairs per bundle) |
| `_anydoc_client.py` | Bounded subprocess client for the private AnyDoc conversion worker |
| `_anydoc_worker.py` | Bounded, offline AnyDoc document-to-Markdown worker |
| `_grep_scan.py` | Isolated, killable regex-scan worker for the `Grep` tool |
| `_html_markdown.py` | Bounded, offline conversion of untrusted HTML into Markdown |
| `_jobs.py` | Shell job registry and process-group lifecycle management |
| `_patch_commit.py` | Guarded commit and rollback for immutable staged patch changes |
| `_patch_parse.py` | Pure, bounded parser for the Nexus multi-file patch format |
| `_patch_stage.py` | Pure snapshot-based planning for parsed Nexus patches |
| `apply_patch.py` | Built-in `apply_patch`: validate and apply bounded multi-file patches |
| `bash.py` | `bash`: run commands and control background jobs in the workspace |
| `bash_output.py` | `BashOutput`: read buffered output and status for a registry-owned job |
| `edit.py` | Built-in `Edit`: exact, single-commit string replacement |
| `glob.py` | Built-in `Glob`: deterministic, workspace-rooted path matching |
| `grep.py` | Built-in `Grep`: deterministic, text-only recursive content search |
| `kill_shell.py` | `KillShell`: terminate a registry-owned background shell job |
| `ls.py` | Built-in `LS`: deterministic, bounded directory listing |
| `meta.py` | The meta builtins: reload, inspect, and author extensions |
| `multiedit.py` | Built-in `MultiEdit`: validate every edit, then commit once atomically |
| `question.py` | `question`: ask the operator one question and wait for the answer |
| `read.py` | Built-in `Read` and the private helpers shared by the fs tools |
| `skill.py` | `Skill`: progressive disclosure of a skill body or bundled resource |
| `task.py` | `subagent` (legacy name `Task`): spawn a bounded subagent (bundle `task`) |
| `todo.py` | `TodoWrite`: agent-scoped in-memory task list (bundle `task`) |
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
| `stream.py` | Turn streaming helpers shared by the one-shot and interactive surfaces |
| `uds.py` | Unix-socket transport for the CLI, over the canonical host wire |

### `nexus/ui/tui/`

| File | Purpose |
| --- | --- |
| `attachments.py` | Pending file and clipboard image attachments, previews, and submission |
| `__init__.py` | Optional Textual shell for the daemon-backed Nexus client |
| `attachments.py` | TUI attachment preparation and Markdown preview through the host |
| `agent_picker.py` | Searchable picker for root agents and selectable models |
| `agent_row.py` | Keyboard and mouse selectable row for one reducer-owned AgentView |
| `agent_transcript.py` | Live sub agent page: the child's session laid out exactly like the root |
| `app.py` | Textual shell |
| `controller.py` | Daemon-client event bridge and reducer seam for the Textual shell |
| `extras.py` | Small, host-backed chat commands kept outside the shell controller |
| `keychord.py` | Ctrl+X leader keys and the "any key stops dictation" rule for the Textual shell |
| `keys.py` | Terminal key-protocol compatibility for the Nexus Textual shell |
| `messages.py` | Typed messages between the event bridge and Textual widgets |
| `mock.py` | `/mock` in the Textual shell (dev mode only) |
| `new_session.py` | `/new`: pick the root agent a new session starts with |
| `panels.py` | Side panels, Sessions dialog, and Settings wiring for the Textual shell |
| `permission.py` | Attended approval and question prompts; arbitration stays daemon-side |
| `run.py` | Small runtime-free entry seam for launching the optional Textual app |
| `theme.py` | Nexus Textual themes: an opencode-style dark workbench and its light twin |
| `timeline.py` | Reducer-backed conversation timeline and compact tool activity rows |
| `tool_details.py` | Modal inspection for a single reducer-backed tool call |
| `usage.py` | Provider usage modal: plan limits for every connected provider (Ctrl+U, `/usage`) |
| `widgets.py` | Compatibility exports for the Textual widget toolkit |

### `nexus/ui/web/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Static browser client for the Nexus daemon |
| `js/fuzzy.js` | Fuzzy matcher for the palette and model picker (port of `ui_support/fuzzy.py`) |
| `js/voice-strip.js` | Floating dictation waveform and bounded live transcript preview |

### `nexus/ui_support/`

| File | Purpose |
| --- | --- |
| `voice_settings.py` | Host-backed voice configuration shared by terminal surfaces |
| `shortcuts.py` | Shared terminal shortcut and leader reference |
| `settings_help.py` | One-line help per Settings area, shared by both consoles |
| `session_status.py` | Shared session-card status words, relative age and sub-line |
| `session_groups.py` | Shared project and local-date grouping for terminal session lists |
| `session_controller.py` | Shared host-client lifecycle, selection and canonical reducer seam |

| File | Purpose |
| --- | --- |
| `__init__.py` | Pure presentation helpers shared by terminal surfaces |
| `agent_frontmatter.py` | Read and rewrite the simple `key: value` frontmatter of an agent `*.md` |
| `clipboard.py` | Bounded local system clipboard image reading for terminal attachment uploads |
| `context.py` | Pure display projections for context preview and session usage |
| `hints.py` | Randomized tips shown in the middle of an empty session (mirrored in `js/hints.js`) |
| `fuzzy.py` | Shared fuzzy matcher (score and match positions) for the command palette and model picker |
| `mock_args.py` | Shared `/mock` argument handling for the chat surfaces |
| `mock_cli.py` | `nexus mock list\|run\|clean` (dev mode only) |
| `prompts.py` | UI-neutral choices for operator prompts: approvals and agent questions |
| `text.py` | Control-safe, credential-redacted text for terminal presentation |
| `timeline.py` | Pure formatting and filtering for reducer-backed conversation timelines |
| `tool_details.py` | Presentable tool call details: every parameter and output, none of the JSON |
| `usage.py` | Provider usage formatting shared by surfaces (`ProvidersUsageResult`) |
| `tui_archived.py` | Search and resume durable archived sessions through host callbacks |
| `tui_command_palette.py` | Textual command-palette entries and the keyboard shortcut reference |
| `tui_context_header.py` | Scrollable request-context header and read-only detail dialogs |
| `tui_diff.py` | Inline file diffs under Edit and Patch activity rows (textual-diff-view) |
| `tui_history.py` | Bounded per-user prompt history for the terminal composer |
| `tui_list.py` | Shared list presentation for inline completions and pickers |
| `details.py` | Toolkit-free details sidebar data (session rows, modified files, MCP rows) shared by both shells |
| `context_header.py` | Toolkit-free context header blocks, agent colors and tool grouping shared by both shells |
| `completion.py` | Toolkit-free composer completion shared by the native shell |
| `model_choice.py` | Toolkit-free model picker sort, fuzzy rank, grouping and effort rules |
| `tui_model_picker.py` | Searchable, grouped terminal model selector |
| `tui_panels.py` | Side panels and the Settings screen for the Textual shell |
| `tui_providers.py` | Settings → Providers pane: sign in to Codex, GitHub Copilot and OpenCode Go |
| `tui_settings.py` | Full-screen Settings page backed entirely by host inventory commands |
| `tui_setup.py` | First-run setup: connect a provider, then chat |
| `tui_voice.py` | Textual dictation controls and consent flow |
| `tui_widgets.py` | Small Textual-only presentation widgets for the Nexus shell |
| `voice_capture.py` | Bounded 16 kHz microphone capture for TUI dictation |

### `nexus/view/`

| File | Purpose |
| --- | --- |
| `__init__.py` | The pure view layer |
| `fold.py` | Delta accumulation, dedup, and log folding for the pure view reducer |
| `model.py` | Renderable view-model types for the pure event reducer |
| `reduce.py` | The pure, synchronous event reducer |

### `nexus/voice/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Local-only voice inference with bounded, transient audio processing |
| `audio.py` | Strict bounded PCM WAV handling for voice input |
| `engine.py` | Local Parakeet Redux adapter |
| `manager.py` | Serialized voice lifecycle and inference |
| `model.py` | Voice lifecycle and transcription values |
| `store.py` | Pinned, bounded local voice model storage |


### `nexus/ui/ratatui/`

| Module | Responsibility |
| --- | --- |
| `__init__.py` | Experimental native surface package |
| `prototype.py` | Host adapter and labelled snapshot projection |

The Rust client lives in `rust/tui/src/`: `main.rs` (terminal loop, key and mouse
handling), `input.rs` (action writers, editor keys, picking, OSC 52 base64),
`bridge.rs` (the versioned snapshot contract), `editor.rs` (grapheme editor),
`render.rs` (palette, layout regions, the draw pass), `render/chrome.rs` (top bar,
tabs, sessions and details sidebars), `render/dialogs.rs` (dialog frames, toned
panel text, Settings area list, prompt and logs regions, completion popup),
`transcript.rs` (blocks to rows, diffs) and `markdown.rs`. `main()` is still one long
loop over local state; splitting it further needs a state struct and is not done.
| `actions.py` | Native shell slash dispatch, attachments and host-backed panels |
| `controller.py` | Continuous native subscription using canonical bootstrap and reduction |
| `preferences.py` | Textual-compatible native shell preferences |
| `workflows.py` | Settings, provider, context, session and worktree workflows |
| `logs.py` | Bounded paged native diagnostics with routine-entry folding |
| `voice.py` | Bounded native dictation using shared capture and host transcription |
| `desktop.py` | Explicit clipboard operations with byte/time bounds |
| `run.py` | Native launch seam and binary discovery |


| Native module | Contract |
| --- | --- |
| `rust/tui/src/trace.rs` | Bounded opt-in native timing samples, percentile summaries and exit report |

The manual `tests/ratatui_performance_check.py` script measures controlling-PTY
streaming, input and CPU cost.

