# Core: runtime, managers, host

How a prompt becomes a turn, and where each piece lives. Paths are relative to
`nexus/`. For design rationale see `ARCHITECTURE.md`. For extension recipes see
`EXTENDING.md`.

## Request path in one picture

```
client (TUI / CLI / browser)
  └─ host/protocol.py command  ──►  host/daemon.py  (one per workspace, UDS + optional HTTP)
                                      └─ host/facade.py  HostFacade  ← the only surface API
                                           ├─ host/supervisor.py   global turn cap, per-session FIFO
                                           ├─ host/presence.py     viewers, first-responder approvals
                                           └─ runtime.py  Runtime  owns every manager
                                                └─ core/loop.py  assemble → stream → gate → dispatch → repeat
                                                     ├─ model/        Provider adapters, router, registry
                                                     ├─ context/      request assembly + budget
                                                     ├─ tools/        tool manager, permissions, builtins
                                                      └─ session/      SQLite append-only records
events ──► session records ──► view/reduce.py (pure) ──► ConversationView ──► every UI
```

## Layers and where things are

| Layer | Package | Key files |
| --- | --- | --- |
| L0 | `config/`, `errors.py`, `events.py`, `util.py` | `config/schema.py` (v2 `msgspec` config: `ModelSection`, `PermissionsSection`, `MCPSection`…), `config/layers.py` (merge order), `events.py` (the `Event` envelope, the public UI boundary) |
| L0½ | `view/` | `reduce.py` (pure reducer `apply`/`fold`), `model.py` (`ConversationView`, `TurnView`, `MessageView`, `ToolCallView`, `PermissionView`, `AgentView`…), `fold.py` |
| L1 | `model/` | `message.py` (message IR), `provider.py` (Provider protocol), `stream.py` (stream events + tool-call accumulator), `providers/*.py` (anthropic, openai, gemini, ollama, opencode, **scripted** for tests), `router.py`, `registry.py` (models.dev catalogue), `tiers.py`, `reasoning_effort.py`, `selection.py` |
| L2 | `core/` | `loop.py` (the agentic loop), `turn.py` (turn state, limits, usage), `bus.py`, `cancel.py`, `registry.py`, `watch.py` |
| L3 | managers | see the next table |
| L4 | `runtime.py` | composition root: builds managers, providers, router, per-turn tool manager, MCP, agents, hooks, extension rebuilds |
| L4½ | `host/`, `host_support/`, `observability/` | see "Host layer" |
| L5 | `ui/`, `ui_support/`, `client/` | see [textual.md](textual.md) and [web.md](web.md) |

### Managers (L3)

| Concern | Package | Start at |
| --- | --- | --- |
| Sessions: open, fork, replay, delete/trash, export, locks | `session/` | `manager.py` (`SessionManager`, `SessionSummary`, `_state_for` gives idle/running/awaiting_*), `session.py` (handle), SQLite record store (`EventRecord`, `MessageRecord`), JSONL export, `snapshot.py` |
| Context assembly and budget | `context/` | `manager.py`, `parts.py`, `budget.py`, `compact.py`, `counting.py`, `cache.py` |
| Tools and permissions | `tools/` | `spec.py` (tool contract), `manager.py` (dispatch), `permissions.py` (rule grammar, path security, approvals), `bundles.py` (profiles), `names.py` (public names), `questions.py` (agent questions), `builtin/*.py` (read, edit, multiedit, write, apply_patch (numbered unified hunks or Codex-style `@@ anchor` context hunks, located by content; `_patch_parse.py` → `_patch_stage.py` → `_patch_commit.py`), bash + jobs, glob, grep, ls, task, todo, skill, webfetch, websearch, meta) |
| Subagents and worktrees | `agents/` | `manager.py`, `model.py` (`*.md` definitions), `runner.py`, `worktrees.py`, `worktree_review.py`, `worktree_integrate.py`, built-in roles in `agents/data/*.md` (root `build`; subagents `advisor` (read-only), `task`, `quick`; user overrides in `~/.nexus/agents/`) |
| Skills | `skills/` | `manager.py`, `frontmatter.py`, `activation.py` |
| Hooks | `hooks/` | `manager.py`, `model.py` |
| MCP | `mcp/` | `manager.py` (`MCPServerStatus`, `MCPHealth`, `statuses()`), `client.py` (stdio/http/sse, `parse_server_config`), `bridge.py` |
| Hot extensions | `ext/` | `manager.py` (atomic rebuild), `manifest.py`, `quarantine.py`, `tools/loader.py` |
| Outbound network | `net/outbound.py` | pinned, public-address-only HTTP for webfetch/websearch |
| Auth | `auth/` | keychain-backed provider sign-in: `codex.py` (ChatGPT OAuth, browser PKCE or device code), `copilot.py` (GitHub.com device flow through the Nexus OAuth app; GitHub token in the keychain, short-lived Copilot token in memory; live Copilot exchange compatibility unverified), `api_key.py` (pasted keys such as OpenCode Go), `store.py` (secure keyring only) |

## Host layer (the surface every UI talks to)

Configuration is loaded in this order: `~/.nexus/config.toml`, the exact
workspace's `nexus.toml`, legacy `.nexus/nexus.toml`, then `.agents/nexus.toml`
(highest precedence). Parent directories and Git roots do not contribute
configuration, so global model and agent defaults apply consistently regardless
of the current working directory; an exact workspace config can override them.

On first launch without a connected global provider/model, `SetupStatus` offers
packaged candidate models and local connection instructions. `SetupSave`
validates the choice and writes `[providers.*]` and `[models].default` to
`~/.nexus/config.toml` through the host settings path. Credentials remain in
the OAuth store or daemon environment and never enter setup commands. The
running daemon needs a restart after saving because provider routes are built
at startup.

Settings → Providers connects ChatGPT (Codex), GitHub Copilot and OpenCode Go
through `ProvidersStatus`, `ProviderLogin` + `ProviderLoginPoll`/`Cancel`,
`ProviderKeySet` and `ProviderLogout` (`host_support/provider_auth.py`). A
browser or device sign-in runs as a bounded daemon task and returns only its URL
and user code. `ProviderKeySet` is the one command that carries a credential,
inward only; it is never echoed, logged or written to config. Connecting writes
`[providers.<id>]` (`auth = "chatgpt_oauth" | "github_copilot" | "keychain"`)
to `~/.nexus/config.toml` and, when no turn is running, rebuilds the routes in
place (`Runtime.reload_model_routes`); otherwise restart the daemon. Copilot
device login is currently GitHub.com-only; older v1 stored tokens must be
reconnected. GitHub OAuth access tokens from apps configured to expire require
another sign-in on expiry. The GitHub token is the Copilot API bearer directly
(no `copilot_internal` exchange) and is checked against `/models` at login; the
device flow uses OpenCode's OAuth app id. This has not yet been verified against
a live account.

| File | Role |
| --- | --- |
| `host/protocol.py` | Wire contract: frozen, tagged `msgspec` commands and `*Result` structs, `PROTOCOL_VERSION`, `decode_command`. Commands include `SessionList/Open/Start/Enqueue/Cancel/Subscribe/State/Fork/Delete/Restore/Export`, `PermissionResolve`, `ModelsList/ModelSelect/ReasoningEffortSelect`, `AgentsList` (its `default` is `[agent] name`, the agent new sessions start with)`/AgentCurrent/AgentSelect/AgentReset/AgentDefaultSet` (writes `[agent] name` via `host_support/settings_inventory.py`), `ToolsList`, `ContextInspect`, `FileSearch`, `LogsRead`, `Worktree*`, `Doctor` (includes MCP status), `Health`, `WebLaunch`, `Shutdown`. |
| `host/facade.py` | `HostFacade`: implements every command (`handle`), `list_sessions`, `start_turn`, `resolve_permission`, `delete`/`restore`, `doctor` (+ `_mcp_report`), `web_snapshot` and `subscribe_workspace` for the browser. |
| `host/daemon.py` | Process lifecycle, UDS socket + handshake, idle shutdown, `web_launch()` (starts the HTTP listener, mints a one-use URL). |
| `host/supervisor.py` | Turn scheduling under the global concurrency cap. |
| `host/presence.py` | Viewer counts and permission leases. Without viewers, the session's `on_unattended` policy applies. |
| `host/transports/uds.py` | Length-framed JSON over the Unix socket (`UDSClient.connect(...).call(cmd)`). |
| `host/transports/http_sse.py` | HTTP commands + SSE events. It also dispatches browser routes to `host/web.py`. |
| `host/web.py` | Browser auth (ticket → cookie → CSRF), static files, `/v1/web/*` routes. See [web.md](web.md). |
| `host_support/` | Read-only projections kept out of `host/` for budget and clarity: `browser_view.py` (web snapshot + JSON patches), `context_preview.py`, `approval.py`, `workspace.py` (file search), `worktree_projection.py`. |
| `observability/` | Bounded daemon and session log projections behind `LogsRead`. |
| `client/protocol.py` | Transport-neutral `Client` used by the CLI and TUI. |

### Adding a capability a UI can use

1. Add `FooCommand` and `FooResult` structs to `host/protocol.py`, and register them in the command and result unions.
2. Handle the command in `HostFacade.handle` (`host/facade.py`). Return redacted, bounded data.
3. Expose it on `client/protocol.py` if the TUI or CLI needs it. The browser calls `api.command({type:'FooCommand', …})` directly.
4. Tests: `tests/test_host_facade.py` style for the facade, and `tests/test_web_transport.py` if the browser uses it.

The browser may send any command except `Shutdown` and `WebLaunch` (`host/web.py`).

### Durable and machine state

All projects share the append-only SQLite database at `~/.nexus/nexus.db`;
record rows retain the existing encoded record format, and JSONL is available
only as an export format. Per-project locks and machine state (cache, logs,
staging, and extension trash) live under `~/.nexus/locks/` and
`~/.nexus/projects/<project-hash>/`; the shared models.dev cache lives at
`~/.nexus/cache/`. Project extensions and project settings are stored in
`<workspace>/.agents/`. Existing `<workspace>/.nexus/` extensions and settings
are read as a lower-precedence legacy fallback, with writes going to `.agents/`.
Legacy session and trash directories are not imported; export sessions before
switching to this storage if they need to be retained.

## The loop, briefly

`core/loop.py` refreshes the extension manifest on every iteration, so tools
written mid-turn are visible on the next iteration. The assistant message is
persisted before any tool runs. The permission gate checks the whole batch
first. Concurrency planning serializes mutating tools. Cancellation is
cooperative. Failure handling and limits are in `core/turn.py`.

Context accounting has two sources. `context.assembled` carries the
assembler's estimate (`used_tokens`, a character heuristic) plus
`input_budget` and `context_window`. `model.usage` carries `prompt`, the
provider's own count of the whole request: an adapter whose `input` excludes
cached tokens sets `usage_input_excludes_cache = True` (Anthropic) and the loop
adds cache reads and writes back. `view/reduce.py` stores that as
`measured_tokens` and carries it into the next assembly (measurement plus the
estimate's growth), so the UI meter (`ui_support/context.py:context_measure`)
shows a provider number whenever one exists.

## Testing the core

- Scripted runs: `ScriptedProvider(text_response(...), tool_response(("id", "Edit", {...})), [MessageStart(...), TextDelta(...), Wait(event), ...])`. Each script is consumed once, in order, across all sessions.
- Full stack in-process: `Runtime(path, config=Config(...), providers={"scripted": provider})`, wrapped in `Daemon(workspace, socket_path=..., runtime_factory=...)`. See `tests/playwright_web_check.py` `main()` for a complete example. **macOS limits Unix socket paths to about 104 bytes**, so keep `socket_path` short or relative.
- Useful suites: `test_core_*`, `test_session_*`, `test_context_*`, `test_model_*`, `test_tool*`, `test_builtin_*`, `test_mcp_*`, `test_host_*`, `test_view_reduce.py`, `test_layering.py`, `test_phase3_exit.py`.

## Dev mode and mock scenarios (`nexus/devtools/`)

`nexus --dev` / `NEXUS_DEV=1` swaps the workspace for a seeded sandbox under an
isolated home and registers `MockProvider` (`mock/<scenario>` models). The
provider is stateless: the actor comes from a `⟦mock …⟧` directive in the first
user message and the step from the number of assistant messages, so parallel
subagents, forks and replays all work. A request without a directive fails
closed. Scenarios are data (`devtools/mock/scenarios/`, DSL in `dsl.py`);
tools run for real inside the sandbox and never touch the network.

Host contract: `MockList`, `MockStart`, `MockClean` (errors outside dev mode),
`HealthResult.dev`. Headless: `nexus mock list|run NAME|all|clean`. Tests:
`tests/test_mock_scenarios.py`, `test_mock_host.py`, `test_mock_tui.py`.

## Releasing

Releases are automatic but never ship on their own (`plans/release.md`). Every change
lands on `main`. On each push, the `release` workflow (release-please) opens or
updates one release PR that bumps `pyproject.toml` and the `nexus-harness` entry in
`uv.lock`, and writes `CHANGELOG.md`. Merging that PR tags `vX.Y.Z`, creates the GitHub
release and publishes to PyPI (trusted publishing, the `pypi` environment).

- **What bumps the version** (from Conventional Commit subjects): `fix:`, `perf:`,
  `deps:` patch; `feat:` minor; a `!` or `BREAKING CHANGE:` footer is a minor bump
  while the version is below 1.0. `docs:`, `chore:`, `test:`, `refactor:`, `ci:`,
  `style:`, `build:` and non-conventional subjects do not release.
- **Force a version:** put `Release-As: 1.0.0` on its own line in a commit body.
- **Hotfix:** push the `fix:` commit, then merge the release PR straight away.
- **Publish failed after the tag exists:** run the `release` workflow by hand
  (`workflow_dispatch`) with the tag; only the publish job runs, and `uv publish`
  refuses a version PyPI already has.
- **Bad release:** PyPI versions are immutable. Yank it on pypi.org, then ship a `fix:`
  release. `install.sh --version X` pins an exact version.
- The workflow file name `release.yml` is bound to the PyPI trusted publisher; don't
  rename it.
- Python 3.13 or newer is required (`object.__setattr__` on msgspec Structs, used
  across the codebase, fails on 3.12 and older).
