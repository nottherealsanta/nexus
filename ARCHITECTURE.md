# Architecture

Nexus is a provider-agnostic agent harness. This document describes the layers,
the five core contracts, the loop, the managers, the host surface, and the
deliberate boundaries. For how to add to it, see [EXTENDING.md](EXTENDING.md).

The design rule that keeps it modular: **`core/loop.py` knows only protocols.**
It talks to a `Provider`, a `ToolDispatcher`, a `ContextAssembler`, a
`PermissionGate`, and an `EventSink`. It imports no concrete manager. That is
what makes the loop testable with fakes and makes every manager replaceable.

## Layering

Strict one-way dependencies. A lower layer never imports a higher one. The
boundary is enforced by tests (`tests/test_layering.py`,
`tests/test_ui_layering.py`), not by convention.

```
    L5  ui/            cli (prompt_toolkit), jsonl           <- surfaces
        ----------------------------------------------------
    L4½ host/          facade, protocol, supervisor, presence, transports
        ----------------------------------------------------
    L4  runtime.py     Runtime: owns managers, wiring, reload
        ----------------------------------------------------
    L3  managers       session/ context/ tools/ skills/ mcp/ agents/ hooks/ ext/
        ----------------------------------------------------
    L2  core/          loop, turn, bus, registry, watch
        ----------------------------------------------------
    L1  model/         message IR, Provider protocol, adapters, registry, tiers
        ----------------------------------------------------
    L0½ view/          pure event reducer  (imports events.py ONLY)
        ----------------------------------------------------
    L0  config, errors, events, util
```

`view/` sits low deliberately: it depends on nothing but the `Event` envelope,
so the daemon, the CLI, and a future frontend all reduce the same stream with
the same code.

The UI encapsulation rule is enforced, not requested: anything under
`nexus/ui/**` may import only `nexus.host`, `nexus.view`, `nexus.events`, and
the standard library.

Two line budgets keep the harness small, reviewed per phase. The §18 amendment
supersedes the original 2,500/2,000-line targets with conservative, rounded
ceilings that keep modest headroom over the as-built tree: `core/` + `model/` +
`tools/spec.py` under **14,000** physical lines and `host/` + `view/` + `ui/`
under **9,500**. The same prefixes and physical-line semantics are kept, and no
code was relocated to evade a cap. The enforcing tests
(`test_line_budget_core_model_spec_within_plan_cap` and
`test_line_budget_host_view_ui_within_plan_cap` in `tests/test_phase3_exit.py`)
are now **strict** — a cap breach fails the suite, and regenerating the closeout
report refuses to write a baseline that reaches or exceeds a cap (the boundary
is `< cap`, so a count exactly at the cap is a violation). At the revision the
measured sizes were 12,560 physical lines for the core tree (~11% headroom) and
8,803 for the surface tree (host 5,198 / view 1,604 / ui 2,001; ~7% headroom).
The committed report (`tests/fixtures/reports/phase3_exit_baseline.json`) pins
that baseline; regenerate it (`NEXUS_PHASE3_WRITE_REPORT=1 pytest
tests/test_phase3_exit.py`) only while the tree is within cap. Anything that
wants to be core *and* live-reloadable is a signal to widen an interface, not to
add a layer.

## The five contracts

### 1. Message IR (`model/message.py`)

Provider-neutral. Everything speaks this.

```python
Text, Thinking(signature), ToolUse, ToolResult, Image, Document
ContentBlock = union of the above
Message(role: "user" | "assistant", content, meta)
```

- **No `system` role.** System instructions are assembled by the context
  manager and passed on `ModelRequest.system`; each adapter places them where its
  API wants them.
- **`Thinking.signature` is opaque and replayed verbatim.** Anthropic requires
  it for multi-turn extended thinking.
- **`ToolResult.content` is a block list**, because tools return images and MCP
  returns mixed content.
- **`meta`** carries provider, model, usage, timestamp, turn id, cache hit, and
  redaction flags for accounting and replay.

Cross-provider history is a real problem with a real answer: each adapter
declares a `Degradation` policy (`drop` / `to_text` / `error`) and emits
`context.degraded` when it cannot represent a block. Switching provider
mid-session is therefore allowed, lossy, and visible.

### 2. Provider protocol (`model/provider.py`)

```python
class Provider(Protocol):
    name: str
    def capabilities(self, model: str) -> Capabilities: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: ModelRequest) -> int | None: ...
    async def aclose(self) -> None: ...
```

`Capabilities` declares tools, parallel tool calls, streaming, thinking, prompt
caching, vision, documents, strict JSON schema, token limits, and the
degradation map. **The loop reads capabilities and adapts** rather than
assuming: if `parallel_tool_calls` is false the loop serializes; if `tools` is
false tool schemas are omitted and a tool-call attempt is a hard, model-visible
error. This is what lets one loop drive a frontier model and a 7B local model.

Capabilities come from the model registry (`model/registry.py`), which is
authoritative; adapters carry only a fallback table. A provider rejection that
contradicts the registry is treated as recoverable: one retry without the
offending feature, a `registry.mismatch` event, and a completed turn.

### 3. Stream events (`model/stream.py`)

Normalized; adapters translate their wire format into exactly these:

```
message_start, text_delta, thinking_delta, thinking_end,
tool_call_start, tool_call_delta, tool_call_end,
usage, message_stop, raw
```

`ToolCallAccumulator` is shared infrastructure: it buffers partial-JSON tool
arguments, parses at call end, and raises a typed `MalformedToolCall` that the
loop converts into a model-visible error result — so the model can self-correct
instead of the turn dying. Anthropic, OpenAI, and every compatible endpoint use
the same accumulator.

### 4. Tool contract (`tools/spec.py`)

```python
ToolSpec(name, description, input_schema, bundle, mutates,
         concurrency, timeout_s, permission_key, max_result_tokens, version)
ToolResult(content: list[ContentBlock], is_error, display, metrics, context_note)
```

`permission_key` is how a tool declares what a permission rule matches:
`Bash` returns the command, `Read`/`Write` return a resolved absolute path, MCP
tools return a server-qualified name. Without it the engine would special-case
every tool.

`context_note` solves tool-result bloat: a 200 KB grep result can be evicted and
replaced with a one-line note instead of being truncated mid-line.

`ToolContext` gives a tool its workspace, session/turn id, config, an emit
callback, a cancel token, `spawn_agent`, and `invoke_tool`. It does **not** give
a tool the runtime. That boundary is what lets a hot-loaded tool be reviewed
rather than trusted blindly.

### 5. Event envelope (`events.py`)

```python
Event(type, data, seq, ts, session, turn, id)
```

Monotonic `seq` per session is the system's spine: it is what makes a
subscription resumable, a log replayable, and `Last-Event-ID` on the wire map
exactly onto disk. The rule is absolute: **every state change a UI could draw is
an event; no UI polls a manager.** UIs must tolerate unknown types.

Groups: session, turn, context, model/text/thinking, tool, permission,
extension, MCP, skill, agent, hook, provider raw, input queue, presence, daemon,
registry, and `error`.

## The loop (`core/loop.py`)

```
append user_input to the session
for iteration in range(limits.max_iterations):
    snapshot = manifest.get()                 # fresh extensions each iteration
    request  = await assemble(session, snapshot)
    provider, model, caps = provider_for(request)
    blocks, usage, stop = await stream_and_collect(...)
    append assistant Message(blocks)          # durable before tools run
    if no tool uses: return stop_reason
    decisions = await gate.check_all(tool_uses, snapshot)
    groups    = plan_concurrency(tool_uses, snapshot, caps)
    results   = await dispatch(groups, tools, emit, cancel)
    append user Message([ToolResult...])
    if budget exceeded: return "budget"
return "max_iterations"
```

Five details matter more than the skeleton:

1. **`manifest.get()` inside the loop** is the whole self-extension story. A
   tool created in iteration N is visible at iteration N+1, same turn, no
   restart.
2. **The assistant message is persisted before tools execute.** A crash leaves
   a dangling `tool_use` with no `tool_result`, which resume synthetically
   closes; the reverse ordering produces history no provider will accept.
3. **Permission checks cover the whole batch before any tool runs**, so one
   approval prompt covers three calls and a denial does not leave half a batch
   executed.
4. **Concurrency planning** groups parallel tools in a task group and runs
   `exclusive`/mutating tools alone, serializing two writes to one path even if
   both claim to be parallel.
5. **Cancellation is cooperative and structured**, checked at every await point;
   tool subprocesses get SIGTERM then SIGKILL by process group.

### Failure handling

| Failure | Response |
| --- | --- |
| Transport / 429 / 5xx | Jittered retry in `model/http.py`, bounded, with `model.retrying`. Never mid-stream. |
| Malformed tool JSON | `tool_result(is_error=True)`; model self-corrects. |
| Unknown tool | Error result listing valid names. |
| Tool raises / times out | Caught, error result; turn continues. |
| Permission denied | Error result naming the rule to grant; turn continues. |
| Context overflow | Compaction; if still over, an actionable turn failure. |
| Provider refusal | `stop_reason="refusal"`, no retry. |

The principle: **tool-level failures become model-visible results; harness-level
failures end the turn.** Never silently swallow either.

## Managers

### SessionManager (`session/`)

Append-only JSONL log plus snapshots. `open`, `fork`, `list`, `delete`,
`restore`, `export`, `replay`. Locking reuses `flock` and atomic rename; a
read-only attach is allowed under a shared lock, while turn execution takes the
exclusive lock. Migration converts the v1 `{exchanges: [...]}` format to a v2
event log on first open, idempotently.

`start_turn()` + `subscribe(from_seq)` split the primitive: a turn runs to
completion unowned, and any number of views catch up from the log then follow.
`send()` remains a wrapper. "Catch up, then follow" is: fold the log to `seq`,
subscribe from `seq + 1`.

### ContextManager (`context/`)

Composable parts with priorities (identity, SOUL, environment, tools, skills
index, MCP index, memory, attachments, history, user). Budget:
`min(config, model) − max_output − safety_margin`. Priority-0 parts are never
dropped. History is a contiguous suffix and compacts itself.

Compaction strategies: `drop_oldest`, `evict_tool_results`, `summarize`,
`hybrid` (default). Compaction writes a new snapshot; the log keeps the
original, so history is always reconstructible.

`context/cache.py` holds a token-count disk cache keyed by a canonical semantic
hash (never prompt content) and the prompt-cache boundary computation. Boundaries
are produced only when the provider advertises `prompt_caching`.

### ToolManager + PermissionEngine (`tools/`)

Registration, schema validation, dispatch with concurrency planning, per-tool
timeouts, result-size caps, and metrics. The permission engine is first-match
`deny → session grants → allow → ask → mode`; `deny` is absolute. `PathGuard`
canonicalizes write roots and read-deny roots before any rule allow, so `../`
and symlink escapes fail closed. The approval flow is UI-agnostic: the gate
emits `permission.requested` with a request id and awaits a future; any UI calls
`resolve_permission`. `*_ALWAYS` persists an exact-action grant (reconstructable
from the session log) or degrades to `*_ONCE`; it can never broaden.

### ExtensionManager (`ext/`)

The only writer of the manifest reference. Every trigger — watcher, the
`ReloadExtensions` tool, or an API call — funnels through one serialized,
coalescing rebuild:

1. load effective config (a bad config is a failed rebuild; the old manifest
   stays);
2. discover tools, skills, agents, hooks, MCP definitions, system files;
3. reuse unchanged extensions by content hash; re-quarantine changed ones;
4. all-or-nothing: any failure releases everything loaded by this attempt and
   keeps the previous manifest;
5. one compare-and-swap of the immutable `ManifestRef`; a no-op does not churn
   the generation;
6. retire a superseded generation only after the last manifest lease is gone.

`trash(target)` is the only operation that removes a *trusted* extension file. It
is scoped to managed roots, refuses symlinks, traversal, and non-candidates,
moves the file atomically into a retention-recorded trash entry, and rebuilds. A
failed rebuild rolls the move back unless `force=True`; a pinned generation keeps
the old module until the last lease releases, then retires it. `nexus ext trash`
exposes it; `nexus ext restore` is not wired to the CLI.

### SkillManager, AgentManager, HookManager, MCPManager

- **Skills** discover `SKILL.md`, validate frontmatter, snapshot bodies, and
  expose only `name: description` to the index. Bodies load on invocation.
- **Agents** parse a restricted frontmatter grammar (no YAML dependency), seed
  `general` / `explore` / `planner`, and enforce read-only roles structurally.
- **Hooks** run command hooks (argv, no shell by default, bounded env) and
  in-process Python hooks (quarantine/loader seam). Events: `hook.fired`,
  `hook.blocked`.
- **MCP** owns lifecycle, health, backoff, circuit breaking, list caching, and
  hot apply. A dead server degrades the turn. Transport, bridge, and manager are
  separate files, and the rest of Nexus never imports the upstream `mcp`
  package.

### Runtime (`runtime.py`)

Owns every manager, the provider set, the router, the tier table, and the model
registry. It builds a fresh per-turn tool manager while sharing one owned job
registry and todo store, so shell jobs and todos survive between turns without a
mutable runtime-global manager.

## The host layer

`nexus/host/` is the transport-neutral surface over one runtime. A UI may import
`nexus.host`, `nexus.view`, `nexus.events`, and nothing else.

- **`facade.py`** is the only surface API. It owns session operations, schedules
  turns through the supervisor, tracks views through presence, and folds the
  session log through the pure reducer to produce a baseline. It never returns a
  credential, environment value, or raw config; errors are redacted and health
  reports counters only.
- **`protocol.py`** is the wire contract: frozen `msgspec` command/result
  structs tagged by type, versioned by `PROTOCOL_VERSION`. The same structs
  serve UDS, HTTP/SSE, and an in-process test.
- **`supervisor.py`** caps concurrently running turns globally, keeps a per-session
  FIFO, and schedules round-robin across sessions. Scheduling never consults a
  subscriber count, so a zero-view turn runs to completion.
- **`presence.py`** counts attached views and gives permission resolutions
  first-responder semantics. Attendance is derived (`viewers > 0`), so a view
  that disconnects mid-turn applies the session's unattended policy instead of
  blocking forever.
- **`daemon.py`** owns the runtime, one deterministic socket per workspace,
  duplicate prevention, stale-socket reclaim, the version handshake, auto-start,
  idle shutdown, and signals. Guarded by an `flock` and a pid file.
- **`transports/uds.py`** is length-framed JSON with a correlated reader loop
  and bounded subscription queues (backpressure to a slow consumer).
- **`transports/http_sse.py`** implements the HTTP/SSE peer transport:
  commands as POST, events as SSE with `Last-Event-ID` mapped one-to-one onto
  the log's `seq`. It is loopback-only, token-authenticated, `Origin`-checked,
  and bounded in every dimension a hostile peer could grow. The daemon serves it
  only as an opt-in, off-by-default surface (`NEXUS_HTTP=1`, the daemon
  entrypoint's `--http` flag, or `Daemon(http=True)`), publishes a mode-`0600`
  discovery file beside the socket, and never logs the token. The root `nexus`
  CLI is a pure client with no switch to enable it, and there is no web frontend.

### `view/`

A pure, synchronous reducer (`apply`, `fold`) that imports only `nexus.events`.
It produces one renderable tree — turns, text, thinking, tool calls with live
status, pending permissions, usage totals — that the CLI renders and a future
HTTP surface serializes. Identical semantics by construction. `nexus sessions
replay` is the same code path, which makes it the primary UI regression harness.

### `ui/cli/`

A pure client: it imports only `nexus.host`, `nexus.view`, `nexus.events`, and
the standard library, and owns no execution path of its own. `details.py` is a
pure function of the reduced `ConversationView` — it formats the phase, the
**effective** model and provider (`model.started`, or the durable
`model.selected` until the next turn reports the actual one), token usage,
context occupancy against the assembled input budget, viewers, queued inputs,
and the subagent tree. The prompt status line, the editor's `bottom_toolbar`,
and `/details` all render from it, so they cannot disagree. Every session,
model, and agent label is sanitized before it is shown. The renderer escapes
control characters and redacts credential shapes before any tool name, key,
error, or permission preview reaches the terminal, and control-escapes streamed
assistant prose while preserving newlines and tabs (a stream arrives one delta at
a time, so a credential shape can be split across fragments; blanket secret
redaction is deliberately not claimed for the stream). Byte payloads show by
size, and ANSI is enabled only on a TTY (`NO_COLOR` by presence or `TERM=dumb`
disable it; `FORCE_COLOR` overrides). Its
replay dedup is tracked **per session**, so switching to a new or forked session
renders its lower `seq` events instead of suppressing them as repeats.
`prompt_toolkit` is the one permitted third-party import and is loaded lazily,
so the client runs with the extra absent (a plain stdin reader), and one-shot
and JSONL runs never build an editor.

## Security model

The daemon exposes a local endpoint that can run `Bash` on the host. That is a
permanent property of the design, not a to-do:

- bind `127.0.0.1` only; the Unix socket is `0600` in a `0700` directory;
- a token generated at daemon start is required by the (non-default) HTTP/SSE
  transport and never logged;
- `Origin` is checked on every HTTP request;
- credentials never traverse the facade in either direction;
- `deny` rules are absolute and evaluated daemon-side, never client-side.

Command-hook and MCP child processes inherit a fixed safe environment plus
explicitly configured names; the host environment is never copied wholesale.
MCP tool descriptions and results are untrusted data wrapped with a no-authority
instruction. Extension files are trusted code; quarantine validates syntax and
imports but does not sandbox Python.

## Self-extension boundaries

Two tiers: **data** (parsed, not imported) and **code** (imported under a
version-stamped module name, never `importlib.reload`, so generations are
immutable). Version-stamped names mean an in-flight call keeps running its
generation while new calls get the next; old generations are dropped once the
last active call releases its lease.

Deliberately **not** hot: core Nexus modules, new pip installs, and the manifest
shape. `nexus doctor --explain-reload` states the boundary. If a capability
keeps wanting to live in core and be hot, the signal is to widen an interface,
not to make core reloadable.

## Model registry and tiers

The registry is data and lookup only; it performs no I/O during a turn. It reads
only descriptive fields from models.dev (ids, names, env var names, npm package
names, modalities, costs, limits) and deliberately drops the rest, so a
third-party catalogue can never redirect a request or supply a credential.

Tier resolution is deterministic:

1. `[models.tiers]` user pin;
2. the curated map shipped with Nexus;
3. blended cost (`input + output/4`), `low <= 2.5 < medium <= 10 < high`;
4. no cost data → `low`.

A tier name is usable anywhere a model reference is. Clamping (`agents.max_tier`,
subagent authority) only ever narrows.

## Testing strategy

- **Provider conformance** — one parametrized suite every adapter passes against
  recorded fixtures: text, single and parallel tool calls, partial-JSON args,
  malformed args, thinking/signature replay, refusal, 429-then-success,
  mid-stream disconnect, usage accounting.
- **Loop** — `ScriptedProvider` plus fake tools: multi-iteration loops,
  `max_iterations`, permission denial mid-batch, timeouts, cancellation at each
  await point, capability degradation, crash-resume.
- **Context / permissions / sessions / hot extension / MCP** — property and
  adversarial suites, including the load-bearing 200-reload leak test and the
  section 6.5 write-a-tool-and-call-it walkthrough.
- **End-to-end** — recorded live runs, network-gated behind `-m live`, excluded
  from CI.

The offline suite runs on Linux and macOS, Python 3.11–3.14.
