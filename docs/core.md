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
                                                     └─ session/      append-only JSONL log
events ──► session log ──► view/reduce.py (pure) ──► ConversationView ──► every UI
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
| Sessions: open, fork, replay, delete/trash, export, locks | `session/` | `manager.py` (`SessionManager`, `SessionSummary`, `_state_for` gives idle/running/awaiting_*), `session.py` (handle), `store.py` (JSONL log, `EventRecord`, `MessageRecord`), `snapshot.py` |
| Context assembly and budget | `context/` | `manager.py`, `parts.py`, `budget.py`, `compact.py`, `counting.py`, `cache.py` |
| Tools and permissions | `tools/` | `spec.py` (tool contract), `manager.py` (dispatch), `permissions.py` (rule grammar, path security, approvals), `bundles.py` (profiles), `names.py` (public names), `questions.py` (agent questions), `builtin/*.py` (read, edit, multiedit, write, apply_patch, bash + jobs, glob, grep, ls, task, todo, skill, webfetch, websearch, meta) |
| Subagents and worktrees | `agents/` | `manager.py`, `model.py` (`*.md` definitions), `runner.py`, `worktrees.py`, `worktree_review.py`, `worktree_integrate.py`, built-in roles in `agents/data/*.md` (root `build`; subagents `advisor` (read-only), `task`, `quick`; user overrides in `~/.nexus/agents/`) |
| Skills | `skills/` | `manager.py`, `frontmatter.py`, `activation.py` |
| Hooks | `hooks/` | `manager.py`, `model.py` |
| MCP | `mcp/` | `manager.py` (`MCPServerStatus`, `MCPHealth`, `statuses()`), `client.py` (stdio/http/sse, `parse_server_config`), `bridge.py` |
| Hot extensions | `ext/` | `manager.py` (atomic rebuild), `manifest.py`, `quarantine.py`, `tools/loader.py` |
| Outbound network | `net/outbound.py` | pinned, public-address-only HTTP for webfetch/websearch |
| Auth | `auth/` | experimental ChatGPT/Codex OAuth credential store |

## Host layer (the surface every UI talks to)

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
