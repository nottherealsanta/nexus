# Architecture

Nexus is a provider-agnostic agent harness. One **daemon per workspace** owns the
runtime, sessions and turns. Three surfaces are pure clients of it: a Textual
chat app (`nexus chat`), a one-shot CLI and JSONL stream (`nexus run`), and a
plain-JS browser app (`nexus web`).

The rule that keeps it modular: **`core/loop.py` knows only protocols.** It talks
to a session view, a context assembler, a provider resolver, a tool dispatcher,
a permission gate and an event sink, and imports no concrete manager. That makes
the loop testable with fakes and every manager replaceable.

## Request path

```
client (TUI / CLI / browser)
  └─ host/protocol.py command ──► host/daemon.py      one per workspace: UDS (+ loopback HTTP)
                                    └─ host/facade.py HostFacade   the only surface API
                                         ├─ host/supervisor.py     global turn cap, per-session FIFO
                                         ├─ host/presence.py       viewers, first-responder approvals
                                         └─ runtime.py Runtime     owns every manager
                                              └─ session/session.py  Session.start_turn → core/loop.py run_turn
                                                   assemble ─► stream ─► gate ─► dispatch ─► repeat
                                                   ├─ context/   request assembly + budget
                                                   ├─ model/     providers, router, registry
                                                   └─ tools/     manager, permissions, builtins

loop events ─► session records (SQLite) ─► view/reduce.py (pure) ─► ConversationView ─► every UI
```

## Layers

Strict one-way imports: a lower layer never imports a higher one.

```
L5   ui/ ui_support/ client/     surfaces (Textual, CLI, JSONL, browser)
L4½  host/ host_support/ observability/   facade, protocol, daemon, transports
L4   runtime.py                  composition root: managers, providers, router, reload
L3   session/ context/ tools/ skills/ mcp/ agents/ hooks/ ext/ voice/ auth/ net/   managers
L2   core/                       loop, turn, bus, cancel, registry, watch
L1   model/                      message IR, Provider protocol, adapters, registry, tiers
L0½  view/                       pure event reducer (imports only events.py)
L0   config/ errors.py events.py util.py
```

`view/` sits low on purpose: it depends only on the `Event` envelope, so the
daemon, CLI, browser and replay all fold the same stream with the same code.
`devtools/` is dev-mode only (see [devtools.md](devtools.md)).

Enforced by tests, not convention:

| Rule | Test |
| --- | --- |
| `nexus/model/**` never imports `core`, `session`, `context`, `runtime`, `tools`, `agent`, `cli` | `tests/test_layering.py` |
| `nexus/ui/**` imports only `nexus.host`, `view`, `events`, `client`, `host_support`, `ui_support`, `ui` and the stdlib; Textual/Rich stay in `ui/tui/` and ten `ui_support/tui_*.py` files | `tests/test_ui_layering.py` |
| No line caps. Line counts are recorded for information | `tests/test_phase3_exit.py` |

If something wants to be both core and live-reloadable, widen an interface; do
not add a layer or make core reloadable.

## The five contracts

| # | Contract | File | Essence |
| --- | --- | --- | --- |
| 1 | Message IR | `model/message.py` | `Text`, `Thinking(signature)`, `ToolUse`, `ToolResult`, `Image`, `Document`; `Message(role: user\|assistant, content, meta)`. No `system` role: the context manager builds system text and each adapter places it. `Thinking.signature` is opaque and replayed verbatim. |
| 2 | Provider protocol | `model/provider.py` | `name`, `capabilities(model)`, `stream(req)`, `count_tokens(req)`, `aclose()`. The loop reads `Capabilities` and adapts. |
| 3 | Stream events | `model/stream.py` | `MessageStart`, `TextDelta`, `ThinkingDelta`, `ThinkingEnd`, `ToolCallStart/Delta/End`, `Usage`, `MessageStop`, `Raw`. `ToolCallAccumulator` assembles partial-JSON arguments for every adapter. |
| 4 | Tool contract | `tools/spec.py` | `ToolSpec` (model-facing declaration plus harness-only fields) and `ToolExecutionResult`. `ToolContext` deliberately has no `Runtime`. |
| 5 | Event envelope | `events.py` | `Event(type, data, seq, ts, session, turn, id)`. Monotonic `seq` per session is the spine. |

Details: [models.md](models.md), [tools.md](tools.md), [events-and-view.md](events-and-view.md).

## Package map

| Package | Owns | Doc |
| --- | --- | --- |
| `config/` | v2 layered config, path and state-dir resolution | [config.md](config.md) |
| `events.py`, `view/` | event catalogue; pure reducer and view model | [events-and-view.md](events-and-view.md) |
| `model/` | IR, providers, router, registry (models.dev), tiers, tokenizer | [models.md](models.md) |
| `auth/` | keychain-backed provider sign-in (Codex, Copilot, API keys) | [models.md](models.md#authentication) |
| `core/` | the agentic loop, turn state, bus, cancellation, registries, watcher | [loop.md](loop.md) |
| `context/` | parts, budget, compaction, token counting, prompt-cache boundaries | [context.md](context.md) |
| `session/` | SQLite records, handle, locks, snapshots, export, archive/trash | [sessions.md](sessions.md) |
| `tools/` | spec, manager, permissions, bundles, builtins, loader | [tools.md](tools.md) |
| `agents/` | subagent definitions, runner, git worktrees | [agents.md](agents.md) |
| `skills/`, `hooks/`, `mcp/`, `ext/` | skills, lifecycle hooks, MCP, hot-reload manifest | [extensions.md](extensions.md) |
| `voice/` | local dictation | [voice.md](voice.md) |
| `net/` | pinned public-address-only HTTP | [security.md](security.md) |
| `runtime.py` | composition root | below |
| `host/`, `host_support/`, `observability/` | facade, protocol, daemon, transports, projections | [host.md](host.md) |
| `client/`, `ui/`, `ui_support/` | surfaces | [cli.md](cli.md), [surfaces.md](surfaces.md), [textual.md](textual.md), [web.md](web.md) |
| `devtools/` | mock provider and scenarios (dev mode) | [devtools.md](devtools.md) |

## Runtime (`runtime.py`)

The composition root and the largest module. `Runtime` builds and owns the
managers, providers, router, tier table, model registry, MCP, agents, hooks and
extension rebuilds. Its rules:

- **Injectable seams.** Tests pass `providers`, `router`, `context`, `sessions`,
  a `ToolManager`/`PermissionEngine` or a `tool_factory`; injected parts are not
  owned or closed by the runtime.
- **Frozen per turn.** One config load feeds the context snapshot and the tool
  snapshot, so catalogue, schemas, path guard, permission policy and limits
  cannot change mid-turn; the next turn reloads.
- **Secrets resolve at use.** Credential references (`${env:NAME}`) stay opaque
  strings until the adapter builds a request. The runtime never reads, logs or
  persists a secret.
- **Per-turn tool manager, shared state.** Each turn builds a fresh `ToolManager`
  while sharing one shell `JobRegistry` and `TodoStore`, so jobs and todos
  survive between turns without a mutable runtime-global manager.
- **Adapters live here.** `_ToolDispatcherAdapter` and `_PermissionGateAdapter`
  implement the loop's protocols so `core/loop.py` stays protocol-only.
- **Child runtimes.** `_ChildRuntime` builds the nested session, tools, path guard
  and permissions for a subagent ([agents.md](agents.md)).

## Shared primitives

| Where | What |
| --- | --- |
| `errors.py` | one taxonomy under `NexusError`: `ConfigError`, `ProviderError` (`MalformedToolCall`), `ToolError`, `ExtensionError` (`ManagerClosed`, `ExtensionTrashError`, `ManifestError` → `StaleGenerationError`), `SessionError` (`SessionBusy`), `BusClosed`, `OperationCancelled` |
| `util.py` | `redact_secrets` and `redact_url_userinfo` (applied on every log line, error and peer-visible string), `new_id` (UUIDv7-shaped) |
| `core/bus.py` | `Bus`: per-subscriber bounded buffers with an explicit overload policy (`drop_oldest` / `drop_newest`, drops are counted); closing drains what is buffered. The session's persistent bus outlives any single turn |
| `core/cancel.py` | `CancelToken`: cooperative, checked at await points |
| `core/registry.py` | immutable generation-stamped `Registry` plus an atomic `RegistryRef` |
| `core/watch.py` | mtime+size `DirectoryWatcher` (no inotify dependency) used by the extension manager |

## Invariants worth memorising

1. **Durable log first.** Every state a UI could draw is an event persisted before
   fan-out. Views are reductions; replay reproduces live state. No UI polls a
   manager for state that must survive reconnect.
2. **Assistant message is persisted before any tool runs.** A crash leaves a
   dangling `ToolUse`, which resume closes with an error `ToolResult` without
   executing anything.
3. **Tool-level failures become model-visible results; harness-level failures
   end the turn.** Never swallow either silently.
4. **`deny` is absolute** and evaluated daemon-side. Approvals never broaden.
5. **A turn outlives its viewers.** Closing a UI never cancels work; scheduling
   never consults a subscriber count.
6. **Everything is bounded:** sizes, counts, timeouts, queues, page sizes.
7. **Errors are redacted before crossing the host boundary;** credentials never
   cross it (one inward-only exception: `ProviderKeySet`).
8. **Per-turn freeze.** Config, environment, `SOUL.md`/`MEMORY.md`, tool catalogue
   and manifest generation are pinned per turn or per iteration.
