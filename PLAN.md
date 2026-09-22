# Nexus: Re-Architecture Plan

**From** a thin application boundary around the Codex CLI
**To** a provider-agnostic, self-extending, general-purpose agent harness.

Status: plan. Nothing in here is implemented yet.
Target: Python >= 3.11 (developed on 3.14), macOS + Linux.

---

## 0. Decisions already made

These were settled before writing the plan and are treated as fixed constraints.
Everything below follows from them.

| Decision | Choice |
| --- | --- |
| Dependencies | Curated thin set: `httpx`, `msgspec`, `mcp`. Provider adapters hand-written against REST. No vendor SDKs, no agent frameworks. |
| Tool safety | Permission engine + host execution. Declarative allow/deny/ask rules, path scoping, UI-agnostic approval events. No OS sandbox, no containers. |
| Providers | Anthropic (reference), OpenAI-compatible, Gemini, local (Ollama/llama.cpp), OpenAI Codex **models via API only** (not the CLI), GitHub Copilot, OpenCode. Base structure must make adding more cheap. |
| Self-extension | Declarative-first. Skills/agents/MCP/config/hooks are watched data. New tools are Python files loaded into fresh module namespaces, registry swapped atomically at turn edges. |

### Open assumptions to verify before Phase 7

1. **Copilot** — plan assumes the GitHub Copilot chat endpoint reachable by
   exchanging the OAuth token in `~/.config/github-copilot/` for a short-lived
   Copilot token, then speaking a near-OpenAI dialect. Verify the current
   endpoint, token-exchange flow, and **the licence terms permitting
   non-editor clients** before building this adapter. If terms disallow it,
   drop the adapter; nothing else in the plan depends on it.
2. **OpenCode** — plan assumes the value is reusing OpenCode's model catalogue
   and stored credentials (a gateway/config on disk) rather than shelling out
   to the Go binary. Confirm what the integration surface actually is. If it is
   only a CLI, it becomes a `SubprocessAgentProvider` (same shape as today's
   Codex adapter) instead of a real model provider.
3. **Codex models** — assumed to be OpenAI Responses-API models reachable with
   a normal API key, so they are a *model routing entry* on the OpenAI adapter,
   not a distinct provider.

Both uncertainties are isolated to single files in `nexus/model/providers/`.

---

## 1. Why re-architect at all

Today Nexus is ~725 lines that do `load -> build context -> stream provider -> save`.
That is a clean design, but every capability the harness is supposed to own now
lives inside Codex:

- The **model/tool loop** is Codex's. Nexus never sees a tool call, so it cannot
  gate, log, retry, or extend one.
- The **tool set** is Codex's. Nexus cannot add a tool, ever.
- **Sandboxing and approvals** are Codex's, and Nexus disables approvals outright.
- **Context** is a single JSON string of `{instructions, memory, history, user}`.
  Assistant turns collapse to one text blob; tool calls, thinking, and images
  have nowhere to live.
- **Sessions** are a whole-file rewrite of `list[Exchange]`. No fork, no replay,
  no compaction without data loss.
- **Skills, MCP, subagents, hooks** do not exist, and `SOUL.md` explicitly
  forbids adding them.

The provider boundary is also wrong for the goal. `Provider.stream(prompt: str)`
says "hand a string to something that does everything else". A multi-provider
harness needs `Provider.stream(request: ModelRequest)` where the request carries
structured messages, tool schemas, and sampling params, and the response carries
structured tool calls.

So this is a rewrite of the middle, not a refactor. What survives is the
philosophy, not much of the code.

### What survives from today's code

| Today | Fate |
| --- | --- |
| `events.py` `Event` dataclass | **Keep the idea**, widen it (ids, seq, ts, session/turn). Still the UI boundary. |
| `store.py` atomic write + `flock` | **Keep the mechanisms**, rehome into `session/store.py` + `session/lock.py`. |
| `config.py` load-and-validate-per-turn | **Keep the discipline**, replace flat schema with layered sections. |
| `context.py` `build_context` | **Replace.** Becomes one strategy inside `ContextManager`. |
| `agent.py` loop | **Replace.** Becomes `core/loop.py`, which now owns tool iteration. |
| `provider.py` `CodexProvider` | **Retire** at Phase 7. Kept working until then as a legacy shim so the repo is never broken. |
| `cli.py` | **Rewrite** as one of several UI adapters over the same event stream. |
| `SOUL.md` | **Rewrite.** Its current text forbids most of this plan. |
| `tests/` | Rewrite alongside. The fake-Codex-executable tests go away with the adapter. |

### Non-goals

Stated up front so the plan does not drift:

- No OS sandbox or container isolation (explicitly decided against).
- No vector store, no RAG, no embedding pipeline. Retrieval is a *tool*, and a
  tool can be added later without core changes.
- No web UI. The event stream must make one possible; building one is out of scope.
- No distributed/multi-machine execution. Single host, multiple processes.
- No agent framework abstractions (chains, graphs, planners). The loop is a loop.
- No automatic model-call summarization enabled by default. Compaction is
  explicit and observable.

---

## 2. Target architecture

### 2.1 Layering

Strict one-way dependencies. A lower layer never imports a higher one.

```
    L5  ui/            cli, jsonl, (later) server        <- adapters, swappable
        ------------------------------------------------
    L4  runtime.py      Runtime: owns managers, wiring, reload
        ------------------------------------------------
    L3  managers        session/ context/ tools/ skills/
                        mcp/ agents/ hooks/ ext/
        ------------------------------------------------
    L2  core/           loop, turn, bus, registry, watch
        ------------------------------------------------
    L1  model/          message IR, Provider protocol, adapters
        ------------------------------------------------
    L0  config, errors, events, util
```

The rule that keeps it modular: **L2 `core/loop.py` knows only protocols.**
It talks to a `Provider`, a `ToolDispatcher`, a `ContextAssembler`, a
`PermissionGate`, and an `EventSink`. It imports no concrete manager. That is
what makes the loop testable with fakes and makes every manager replaceable.

### 2.2 File layout

```
nexus/
  __init__.py                 public API re-exports only
  errors.py                   exception taxonomy
  events.py                   Event envelope + EVENT catalogue constants
  config/
    __init__.py
    schema.py                 msgspec Structs for every config section
    layers.py                 defaults <- user <- workspace <- env <- flags
    paths.py                  workspace/user path resolution

  core/
    bus.py                    async fanout, bounded queues, drop policy
    registry.py               generic generation-stamped registry
    watch.py                  mtime-poll directory watcher (no deps)
    turn.py                   Turn state machine, StopReason
    loop.py                   THE agentic loop. Protocols only.
    cancel.py                 cooperative cancellation scope

  model/
    message.py                Message, ContentBlock union (the IR)
    request.py                ModelRequest, ToolSchema, SamplingParams
    stream.py                 StreamEvent union, ToolCallAccumulator
    capabilities.py           Capabilities descriptor
    provider.py               Provider protocol, ProviderError taxonomy
    router.py                 "model string" -> (provider, model, params)
    tokenizer.py              Tokenizer protocol + heuristic default
    http.py                   shared httpx client, retry/backoff, SSE reader
    providers/
      __init__.py             entry-point + file discovery registration
      anthropic.py
      openai.py               OpenAI + any OpenAI-compatible base_url
      gemini.py
      ollama.py
      copilot.py              (gated on assumption #1)
      opencode.py             (gated on assumption #2)
      scripted.py             deterministic test provider
      legacy_codex_cli.py     today's adapter, retired at Phase 7

  session/
    manager.py                SessionManager: open/fork/list/delete/resume
    session.py                Session handle: send(), cancel(), resolve()
    store.py                  append-only JSONL log + snapshot
    lock.py                   flock-based cross-process lock
    migrate.py                v1 {exchanges:[]} -> v2 event log

  context/
    manager.py                ContextManager.assemble()
    parts.py                  ContextPart protocol + builtin parts
    budget.py                 budget allocation + accounting
    compact.py                compaction strategies
    cache.py                  prompt-cache breakpoint placement

  tools/
    manager.py                ToolManager: registry, dispatch, concurrency
    spec.py                   ToolSpec, ToolCall, ToolResult
    permissions.py            PermissionEngine, Rule, Decision
    loader.py                 hot-load .py tool modules
    bundles.py                named groups: fs, shell, net, task, memory, meta
    builtin/
      read.py write.py edit.py multiedit.py
      glob.py grep.py ls.py
      bash.py bash_output.py kill_shell.py
      todo.py
      task.py                 spawn subagent
      skill.py                invoke a skill
      web_fetch.py web_search.py
      memory.py
      meta.py                 ReloadExtensions, ListExtensions, WriteTool

  skills/
    manager.py                discovery, frontmatter, progressive disclosure
    model.py                  Skill struct
    resources.py              bundled file/script resolution

  mcp/
    manager.py                server lifecycle, health, restart backoff
    client.py                 stdio / streamable-http / sse transports
    bridge.py                 MCP tools|resources|prompts -> Nexus objects

  agents/
    manager.py                subagent definitions from agents/*.md
    runner.py                 nested Runtime, isolated session + tool set

  hooks/
    manager.py                lifecycle hooks, command + in-process
    model.py                  HookSpec, HookDecision

  ext/
    manager.py                ExtensionManager: the reload orchestrator
    manifest.py               immutable snapshot of all live extensions
    quarantine.py             validate-before-swap

  runtime.py                  Runtime
  ui/
    cli.py                    argparse CLI + interactive REPL
    render.py                 terminal rendering of the event stream
    jsonl.py                  --json passthrough
```

`nexus/` has no `__init__` side effects beyond re-exports. Import cost matters
because subagents spawn nested runtimes.

### 2.3 On-disk layout

```
<workspace>/
  nexus.toml                  workspace config (sectioned, versioned)
  SOUL.md                     system instructions (name kept, content rewritten)
  MEMORY.md                   durable notes, agent-editable
  .nexus/
    sessions/<id>.jsonl       append-only event log
    sessions/<id>.snap.json   periodic snapshot (fast resume)
    sessions/<id>.lock
    mcp.json                  MCP server definitions
    hooks.toml
    tools/*.py                workspace-local tools        [hot]
    skills/<name>/SKILL.md    workspace-local skills       [hot]
    agents/<name>.md          subagent definitions         [hot]
    providers/*.py            workspace-local providers    [hot]
    cache/                    prompt caches, MCP tool lists, token counts
    logs/nexus.log
    trash/                    deleted extensions, kept for one week

~/.nexus/
  config.toml                 user defaults
  credentials.json            0600, never logged, never in context
  tools/  skills/  agents/  providers/     user-global extensions
```

`[hot]` marks directories the watcher observes. Precedence for same-named
extensions: workspace > user > builtin. Shadowing is logged, not silent.

---

## 3. The core contracts

Five contracts. Get these right and the rest is mechanical. Get them wrong and
every manager leaks.

### 3.1 Message IR (`model/message.py`)

Provider-neutral. Everything else in the system speaks this.

```python
class Text(Struct, tag="text"):        text: str
class Thinking(Struct, tag="thinking"):
    text: str
    signature: str | None = None      # opaque provider blob, replayed verbatim
class ToolUse(Struct, tag="tool_use"):
    id: str
    name: str
    input: dict[str, Any]
class ToolResult(Struct, tag="tool_result"):
    tool_use_id: str
    content: list[Text | Image]
    is_error: bool = False
class Image(Struct, tag="image"):
    media_type: str
    data: bytes | None = None
    url: str | None = None
class Document(Struct, tag="document"):
    media_type: str
    data: bytes
    title: str | None = None

ContentBlock = Text | Thinking | ToolUse | ToolResult | Image | Document

class Message(Struct):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    # harness-only metadata, never sent to a provider
    meta: MessageMeta = field(default_factory=MessageMeta)
```

Design notes worth defending:

- **No `system` role.** System instructions are assembled by `ContextManager`
  and passed on `ModelRequest.system`. Providers place them where their API wants
  (top-level `system` for Anthropic, a leading message for OpenAI). Putting
  system text in the message list is the single most common cause of
  provider-adapter bugs.
- **`Thinking.signature` is opaque and replayed verbatim.** Anthropic requires
  it for multi-turn extended thinking. Never rewrite it, never truncate it.
- **`ToolResult.content` is a block list**, not a string, because tools return
  images (screenshots) and MCP returns mixed content.
- **`meta`** carries `provider`, `model`, `usage`, `ts`, `turn_id`, `cache_hit`,
  `redacted` — needed for accounting, replay, and cross-provider history.

**Cross-provider history is a real problem**, so it gets a real answer.
When history contains blocks a provider cannot represent, the adapter applies a
declared `Degradation` policy (`drop` / `to_text` / `error`) and the harness
emits `context.degraded`. Concretely: Gemini has no thinking-signature concept,
so Anthropic thinking blocks degrade to `drop`; a tool-call id format mismatch
is rewritten through a per-adapter id map. Switching provider mid-session is
therefore allowed, lossy, and *visible*.

### 3.2 Provider protocol (`model/provider.py`)

```python
class Provider(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(self, req: ModelRequest) -> int | None: ...
        # None -> caller falls back to Tokenizer heuristic

    async def aclose(self) -> None: ...
```

```python
class Capabilities(Struct, frozen=True):
    tools: bool
    parallel_tool_calls: bool
    streaming: bool
    thinking: bool
    prompt_caching: bool
    vision: bool
    documents: bool
    json_schema_strict: bool
    max_context_tokens: int
    max_output_tokens: int
    degradation: dict[str, Literal["drop", "to_text", "error"]]
```

The loop **reads capabilities and adapts** rather than assuming. If
`parallel_tool_calls` is false, the loop serializes. If `tools` is false, tool
schemas are omitted and a tool-call attempt is a hard error. If `thinking` is
false, thinking blocks are dropped per policy. This is the mechanism that lets
one loop drive a frontier model and a 7B local model.

### 3.3 Stream events (`model/stream.py`)

Normalized. Adapters translate their wire format into exactly these.

```
message_start      {model, provider}
text_delta         {text}
thinking_delta     {text}
thinking_end       {signature}
tool_call_start    {id, name}
tool_call_delta    {id, partial_json}     # OpenAI-style arg streaming
tool_call_end      {id, input}            # accumulator emits parsed input
usage              {input, output, cache_read, cache_write, reasoning}
message_stop       {stop_reason}
raw                {...}                  # opt-in passthrough for debugging
```

`ToolCallAccumulator` is shared infrastructure, not per-adapter code: it buffers
`tool_call_delta` partial JSON, parses at `tool_call_end`, and raises a typed
`MalformedToolCall` that the loop converts into a `tool_result(is_error=True)`
so the model can self-correct instead of the turn dying. Anthropic streams
`input_json_delta` the same way; one accumulator serves both.

`stop_reason` normalizes to: `end_turn | tool_use | max_tokens | stop_sequence |
refusal | error`.

### 3.4 Tool contract (`tools/spec.py`)

```python
class ToolSpec(Struct, frozen=True):
    name: str
    description: str
    input_schema: dict            # JSON Schema, sent to the model
    # --- harness behaviour, never sent to the model ---
    bundle: str                   # fs | shell | net | task | memory | meta | mcp | ext
    mutates: bool                 # affects concurrency + permission default
    concurrency: Literal["parallel", "exclusive"] = "parallel"
    timeout_s: float | None = None
    permission_key: Callable[[dict], str] | None = None
    max_result_tokens: int = 25_000
    version: str = "1"

class ToolResult(Struct):
    content: list[ContentBlock]
    is_error: bool = False
    # harness-only
    display: str | None = None    # what the UI shows instead of raw content
    metrics: dict | None = None
    context_note: str | None = None  # replaces content once evicted

Tool = Callable[[dict, ToolContext], Awaitable[ToolResult]]
```

`ToolContext` gives a tool: `workspace`, `session_id`, `turn_id`, `config`,
`emit(event)` for progress, `cancel_token`, `spawn_agent(...)`, and
`invoke_tool(...)`. It does **not** give a tool the `Runtime`. Tools cannot
reach up the stack — that boundary is what lets a hot-loaded tool be
sandboxed by review rather than by hope.

`permission_key` is how a tool declares what a permission rule matches against:
`Bash` returns the command string, `Read`/`Write` return the resolved absolute
path, MCP tools return the server-qualified name. Without this, the permission
engine has to special-case every tool.

`context_note` solves tool-result bloat properly: a 200 KB grep result can be
evicted from the context window and replaced with
`"[3,412 matches for X, evicted; re-run Grep to see them]"` instead of being
blindly truncated mid-line.

### 3.5 Event envelope (`events.py`)

```python
class Event(Struct, frozen=True):
    type: str
    data: dict[str, Any]
    seq: int                 # monotonic per session
    ts: float
    session: str | None = None
    turn: str | None = None
    id: str = ""             # uuid7, stable across replay
```

Full catalogue, grouped. UIs must tolerate unknown types (already true today).

```
session.opened  session.closed
turn.started    turn.completed   turn.failed   turn.cancelled
context.assembled  context.compacted  context.degraded
model.started   text.delta  text  thinking.delta  thinking
model.usage     model.stopped   model.retrying
tool.requested  tool.started  tool.progress  tool.completed  tool.failed
permission.requested  permission.resolved
ext.loaded  ext.unloaded  ext.failed  ext.manifest_changed
mcp.connected  mcp.disconnected  mcp.failed  mcp.tools_changed
skill.invoked   skill.completed
agent.spawned   agent.completed
hook.fired      hook.blocked
provider.raw
error
```

Rule: **every state change a UI could want to draw is an event.** No UI ever
polls a manager. This is already the strongest property of the existing design
and it is the one to preserve hardest.

---

## 4. The loop (`core/loop.py`)

The heart. Written against protocols, no manager imports.

```python
async def run_turn(
    *,
    session: SessionView,        # read history, append messages
    user_input: list[ContentBlock],
    assemble: ContextAssembler,  # (session, manifest) -> ModelRequest
    provider_for: ProviderResolver,
    tools: ToolDispatcher,
    gate: PermissionGate,
    manifest: ManifestRef,       # <-- re-read each iteration. THE hot-reload hook.
    emit: EventSink,
    cancel: CancelToken,
    limits: TurnLimits,
) -> TurnOutcome:
```

```
append user_input to session
for iteration in range(limits.max_iterations):

    snapshot = manifest.get()              # (1) fresh extensions every iteration
    request  = await assemble(session, snapshot)
    emit context.assembled

    provider, model, caps = provider_for(request)
    blocks, usage, stop = await stream_and_collect(provider, request, emit, cancel)

    append assistant Message(blocks) to session     # (2) durable before tools run

    tool_uses = [b for b in blocks if isinstance(b, ToolUse)]
    if not tool_uses:
        return TurnOutcome(stop_reason=stop, ...)

    decisions = await gate.check_all(tool_uses, snapshot)   # (3) may emit+await
    groups    = plan_concurrency(tool_uses, snapshot, caps) # (4)
    results   = await dispatch(groups, tools, emit, cancel)

    append user Message([ToolResult...]) to session
    if limits.exceeded(usage_total, wall_clock):
        return TurnOutcome(stop_reason="budget")

return TurnOutcome(stop_reason="max_iterations")
```

Five details that matter more than the skeleton:

1. **`manifest.get()` inside the loop** is the whole self-extension story.
   A tool created during iteration N is visible to the model at iteration N+1,
   in the same turn, with no restart. Everything in section 6 exists to make
   this line safe.

2. **Assistant message is persisted before tools execute.** If a tool crashes
   the process, resume finds a dangling `tool_use` with no `tool_result` and
   synthesizes an error result. Without this ordering, crash recovery produces
   history that no provider will accept.

3. **Permission checks happen for the whole batch before any tool runs.** A
   single approval prompt covering three calls beats three sequential prompts,
   and a denial does not leave half the batch already executed.

4. **Concurrency planning** groups by `concurrency`/`mutates`: parallel tools
   run in a `TaskGroup`; `exclusive` tools run alone in declaration order.
   Two writes to the same resolved path are serialized even if both say
   `parallel`.

5. **Cancellation is cooperative and structured.** `CancelToken` is checked at
   every await point; tool subprocesses get SIGTERM then SIGKILL in a process
   group (the one piece of today's `provider.py` worth copying wholesale).

### Failure handling, explicitly

| Failure | Response |
| --- | --- |
| Transport error / 429 / 5xx | Retry with jittered backoff in `model/http.py`; emit `model.retrying`. Bounded, then fail the turn. |
| Malformed tool JSON | `tool_result(is_error=True)` with the parse error. Model self-corrects. Counts against a per-turn malformed budget. |
| Unknown tool name | Same — error result listing valid names. |
| Tool raises | Caught, `tool_result(is_error=True)` with traceback summary. Turn continues. |
| Tool times out | Killed, error result. Turn continues. |
| Permission denied | Error result explaining the denial and how to grant it. Turn continues. |
| Context overflow at assembly | Compaction runs; if still over, turn fails with an actionable error (today's behaviour, kept). |
| Provider refusal | `stop_reason="refusal"`, turn completes, no retry. |

The principle: **tool-level failures become model-visible results; harness-level
failures end the turn.** Never silently swallow either.

---

## 5. The managers

### 5.1 SessionManager (`session/`)

Replaces the whole-file `list[Exchange]` model with an **append-only event log
plus snapshots**.

```
.nexus/sessions/main.jsonl       one JSON object per line, fsync'd, never rewritten
.nexus/sessions/main.snap.json   {seq, messages[], summary?, usage}
```

Why a log: fork, replay, and compaction all become cheap and lossless.
Compaction writes a *new* snapshot; the log still holds the original messages.
"Omission from the prompt does not delete history" — today's promise — gets
stronger, not weaker.

API:

```python
sessions.open(id, *, create=True) -> Session
sessions.fork(id, at_seq=None) -> Session       # branch a conversation
sessions.list() -> list[SessionInfo]
sessions.delete(id)
sessions.replay(id) -> AsyncIterator[Event]     # rebuild a UI from disk
```

`Session` is the public handle:

```python
async for ev in session.send("do the thing"): ...
await session.resolve_permission(req_id, Decision.ALLOW_ONCE)
session.cancel()
await session.compact(strategy="summarize")
```

Locking: keep today's `flock` + atomic-rename mechanics verbatim. Extend so a
**read-only attach** (a second UI watching a running session) is allowed via a
shared lock; only turn execution takes the exclusive lock.

Migration (`session/migrate.py`): v1 `{version:1, exchanges:[{user,assistant}]}`
converts on first open to a v2 log of alternating messages. Idempotent, keeps a
`.v1.bak`. Tested against real files from the current format.

### 5.2 ContextManager (`context/`)

Today: one function, one budget, drop oldest exchanges.
Target: composable *parts*, priority budgets, token-aware, cache-aware.

```python
class ContextPart(Protocol):
    name: str
    priority: int          # 0 = never drop
    async def render(self, ctx: AssemblyContext) -> PartOutput | None
```

Builtin parts, in assembly order (order is fixed because prompt caching depends
on a stable prefix):

| # | Part | Priority | Content |
| --- | --- | --- | --- |
| 1 | `identity` | 0 | Harness preamble, capabilities, current date |
| 2 | `soul` | 0 | `SOUL.md` |
| 3 | `environment` | 1 | cwd, platform, git branch/status, workspace profile |
| 4 | `tools` | 0 | Tool schemas (on `ModelRequest.tools`, not in text) |
| 5 | `skills_index` | 1 | Name + description only, one line each |
| 6 | `mcp_index` | 2 | Connected servers, resource roots |
| 7 | `memory` | 1 | `MEMORY.md` |
| 8 | `attachments` | 2 | Explicitly pinned files |
| 9 | `history` | 3 | Message suffix, oldest-droppable |
| 10 | `user` | 0 | Current input |

Budget algorithm:

1. Total = `min(config.context_tokens, caps.max_context_tokens) - caps.max_output_tokens - safety_margin`.
2. Render all priority-0 parts. If they alone exceed total -> fail with an
   actionable error naming the oversized part. (Today's promise, better message.)
3. Allocate remaining budget by priority, each part capped by
   `config.context.limits.<part>`.
4. `history` gets the remainder and compacts itself to fit.

Token counting (`model/tokenizer.py`): use `provider.count_tokens()` when the
provider offers it (Anthropic does), cache results by content hash in
`.nexus/cache/`, and fall back to a calibrated heuristic
(`chars/3.7` for prose, `chars/2.9` for code/JSON) otherwise. Budgets are
advisory with a safety margin; the harness must never hard-fail because an
estimate was 4% off.

Compaction strategies (`context/compact.py`), all explicit and observable:

- `drop_oldest` — today's behaviour, the default. Whole messages, never partial.
- `evict_tool_results` — replace old large `ToolResult` content with its
  `context_note`. Runs before dropping messages; usually enough.
- `summarize` — one model call summarizing the dropped prefix into a pinned
  `Text` block. Opt-in, emits `context.compacted` with token counts, records
  the summary in the session log so it is reproducible.
- `hybrid` — evict, then summarize, then drop.

Prompt caching (`context/cache.py`): place breakpoints after part 4 (stable
system+tools prefix) and after the last stable history boundary. Only active
when `caps.prompt_caching`. This is the difference between an affordable
long-session harness and an expensive one, so it is designed in from the start
rather than bolted on.

### 5.3 ToolManager + PermissionEngine (`tools/`)

Manager responsibilities: registry (via `core/registry.py`), schema validation
against the model's declared input schema, dispatch with concurrency planning,
per-tool timeouts, result size capping, and metrics.

**Permission engine.** Rule grammar, deliberately small:

```
Tool                    whole tool, any arguments
Tool(pattern)           glob match against the tool's permission_key
Bundle:name             every tool in a bundle
mcp__server__*          MCP wildcards
```

Config:

```toml
[permissions]
mode = "ask"                       # allow | ask | deny  (default for unmatched)
allow = ["Read(**)", "Glob(**)", "Grep(**)", "Bash(git status)", "Bash(git diff*)"]
ask   = ["Write(**)", "Edit(**)"]
deny  = ["Bash(rm -rf*)", "Read(**/.env)", "Read(**/credentials*)", "Bundle:net"]
write_roots = ["./"]               # hard boundary for any mutating fs tool
read_denyroots = ["~/.ssh", "~/.nexus/credentials.json"]
```

Evaluation order, first match wins: `deny` -> session grants -> `allow` ->
`ask` -> `mode`. **`deny` is absolute** and cannot be overridden by a session
grant or by the model. `write_roots` is checked *after* path resolution
(`realpath`, symlinks followed) so `../` and symlink escapes fail closed.

The approval flow, UI-agnostic:

```
loop            -> gate.check(tool_use)
gate            -> emit Event("permission.requested", {id, tool, key, preview, suggestions})
gate            -> await future[id]                     (respects cancel + timeout)
UI (any kind)   -> session.resolve_permission(id, Decision.ALLOW_ALWAYS)
gate            -> persist grant if *_ALWAYS, emit permission.resolved
```

`Decision`: `ALLOW_ONCE | ALLOW_ALWAYS | DENY_ONCE | DENY_ALWAYS`.
`*_ALWAYS` writes a rule to session state (in-memory + session log) or to
`nexus.toml` if the UI asks for persistence.

Headless behaviour is a config choice, not an accident:
`permissions.on_unattended = "deny" | "allow" | "fail_turn"`, default `deny`
with an error result telling the model exactly which rule to ask the user for.

**Bundles** (`tools/bundles.py`) make the harness general-purpose rather than
coding-specific:

| Bundle | Tools |
| --- | --- |
| `fs` | Read Write Edit MultiEdit Glob Grep LS |
| `shell` | Bash BashOutput KillShell |
| `net` | WebFetch WebSearch |
| `task` | Task (subagents), TodoWrite |
| `memory` | MemoryRead MemoryWrite |
| `meta` | ReloadExtensions ListExtensions WriteTool |
| `mcp` | everything bridged from MCP |
| `ext` | everything hot-loaded from `tools/*.py` |

Profiles compose bundles:

```toml
[profile.coding]   bundles = ["fs","shell","task","meta","mcp","ext"]
[profile.research] bundles = ["fs","net","task","memory","mcp"]
[profile.chat]     bundles = ["memory"]
[profile.ops]      bundles = ["shell","net","task","mcp"]
```

Nothing in `core/` or `tools/manager.py` knows what "coding" means. That is the
general-purpose requirement, enforced structurally.

### 5.4 SkillManager (`skills/`)

A skill is a directory with `SKILL.md`:

```markdown
---
name: risk3-docker-testing
description: Run DS-pack tests locally inside the Risk3 container. Use when...
allowed-tools: [Bash, Read, Glob]
bundles: [fs, shell]
model: inherit
version: 1
---

# body: loaded only when invoked
```

**Progressive disclosure is the entire point.** The context carries only
`name: description` (one line each, part 5 above). The body loads when the model
calls `Skill(name="...")`, which returns the body as a `ToolResult`. A hundred
skills cost ~2k tokens idle instead of blowing the window.

A skill may bundle `scripts/`, `references/`, and `tools/*.py`. Its tools are
registered only while the skill is active in the turn — scoped registration,
handled by the same registry generation mechanism as everything else.

Discovery order: `.nexus/skills/` > `~/.nexus/skills/` > builtin. Directories
are watched; adding a `SKILL.md` makes the skill available on the next loop
iteration.

### 5.5 MCPManager (`mcp/`)

Uses the official `mcp` package (approved in the dependency decision) for
protocol types and client sessions, wrapped so the rest of Nexus never imports
it directly. That wrapper is worth the file: it keeps an upstream breaking
change confined to `mcp/client.py`.

```jsonc
// .nexus/mcp.json
{
  "servers": {
    "github":   { "transport": "stdio", "command": "npx", "args": ["-y","@modelcontextprotocol/server-github"],
                  "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"} },
    "internal": { "transport": "http", "url": "https://mcp.example.com/mcp",
                  "headers": {"Authorization": "Bearer ${env:MCP_TOKEN}"} }
  }
}
```

Responsibilities:

- **Lifecycle** — lazy connect on first use (cold start is the common cost),
  health checks, restart with exponential backoff and a circuit breaker.
- **Failure isolation** — a dead server *never* fails a turn. Its tools vanish
  from the manifest, `mcp.failed` is emitted, and a note goes in the context
  index. This is exactly the failure this very session hit (`github`:
  `CONNECTION_CLOSED`); the harness should degrade, not die.
- **Bridging** — tools become `mcp__<server>__<tool>` in bundle `mcp` with
  `mutates=True` unless annotated read-only. Resources become
  `ReadMcpResource`. Prompts become slash-invocable.
- **Hot add/remove** — editing `mcp.json` spawns or kills servers and swaps the
  manifest. No restart.
- **Tool-list caching** in `.nexus/cache/mcp/` keyed by server version, with
  invalidation on the `tools/list_changed` notification.

Security: env interpolation only via explicit `${env:VAR}`; server stderr
captured to `.nexus/logs/` not to context; **MCP tool descriptions and results
are untrusted data.** They are wrapped in a delimiter and prefaced with a
standing instruction that content inside carries no authority — the prompt
injection surface here is real and belongs in the design, not in a later patch.

### 5.6 AgentManager (`agents/`)

Subagents make this a general harness rather than one assistant.

```markdown
---
name: explorer
description: Read-only search agent for broad fan-out searches.
bundles: [fs]
tools: ["-Write", "-Edit"]
model: sonnet
max_iterations: 30
context_tokens: 100000
---
System prompt for this agent.
```

`Task(subagent_type="explorer", prompt="...")` spawns a **nested Runtime** with
its own session (`<parent>/sub/<n>`, logged under the parent so the whole tree
replays), its own restricted tool set, and its own model. The subagent's events
are re-emitted on the parent bus with an `agent` field so a UI can render a
tree. Its final report comes back as a `ToolResult`.

Guards: `max_depth` (default 3) to stop recursive spawning, an aggregate token
budget across the tree, and a parent-cancel that propagates down.

### 5.7 HookManager (`hooks/`)

Deterministic behaviour the model cannot skip — the thing prompts cannot
guarantee.

Events: `SessionStart`, `UserPromptSubmit`, `ContextAssembled`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `TurnEnd`, `SessionEnd`, `ExtensionLoaded`.

```toml
[[hooks.PreToolUse]]
matcher = "Write(**/*.py)"
type = "command"
command = "ruff check --stdin-filename $NEXUS_TOOL_PATH -"
on_nonzero = "block"        # block | warn | ignore
timeout_s = 10
```

A hook returns `HookDecision`: `allow`, `block(reason)` (becomes an error
`ToolResult` the model sees), or `modify(new_input)`. In-process Python hooks
from `.nexus/hooks/*.py` use the same hot-load path as tools.

---

## 6. Self-extension without reload

The hardest requirement, and the one the architecture is shaped around.

### 6.1 The model

Two tiers:

**Tier 1 — data extensions.** Skills, agents, hooks, MCP servers, `nexus.toml`,
`SOUL.md`, `MEMORY.md`. These are parsed, not imported. "Reloading" is re-reading
a file into a new immutable struct. Zero risk.

**Tier 2 — code extensions.** Tools, providers, in-process hooks: `.py` files in
`.nexus/tools/`, `.nexus/providers/`, `.nexus/hooks/`. Loaded with `importlib`
under a **version-stamped module name**, never `importlib.reload`.

```python
def load_module(path: Path, gen: int) -> ModuleType:
    mod_name = f"nexus_ext.{path.stem}__g{gen}"          # unique per generation
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
```

Why not `importlib.reload`: reload mutates a module in place, so objects already
captured by an in-flight call silently change identity underneath it, and
`isinstance` checks against pre-reload classes start failing. Version-stamped
names give **immutable generations**: an in-flight tool call keeps running
generation 3 while new calls get generation 4. Old generations are dropped from
`sys.modules` once their refcount of active calls hits zero.

### 6.2 The Manifest

```python
class Manifest(Struct, frozen=True):
    generation: int
    config: Config
    tools:     Map[str, RegisteredTool]
    skills:    Map[str, Skill]
    agents:    Map[str, AgentDef]
    hooks:     Map[str, list[HookSpec]]
    providers: Map[str, Provider]
    mcp:       Map[str, MCPServerState]
    system_files: SystemFiles          # soul, memory contents + hashes
```

`ManifestRef` is a single atomic reference (one `object` assignment — no lock
needed for readers). The loop reads it once per iteration.

**Consistency rule: one `Manifest` per loop iteration.** A turn never sees a
half-updated world. The manifest can change *between* iterations — that is the
feature — but never *within* one.

### 6.3 Reload triggers

Three, all landing in the same `ExtensionManager.rebuild()`:

1. **Watcher** (`core/watch.py`) — mtime+size poll of the hot directories every
   `config.ext.watch_interval_ms` (default 500, 0 disables). Deliberately
   dependency-free polling: ~20 directories, negligible cost, no platform
   fsevents/inotify divergence to debug.
2. **Explicit tool** — the model calls `ReloadExtensions()`. Synchronous,
   returns the diff as a `ToolResult` so the model *knows* its new tool is live.
3. **API** — `await runtime.extensions.reload()` for UIs and tests.

### 6.4 Quarantine: validate before swap

A broken hot-loaded tool must not break the harness. `ext/quarantine.py`:

1. **Static checks** — file size cap, `ast.parse` for syntax, a required
   `SPEC`/`register()` symbol, and a warn-list scan for import-time side effects.
2. **Isolated import** — `exec_module` in a subprocess with a short timeout to
   catch import-time hangs and crashes. Result is the extracted `ToolSpec`,
   serialized back. (A hung import in-process would wedge the event loop; this
   subprocess step is why it cannot.)
3. **Schema validation** — `input_schema` must be a valid JSON Schema object
   with `type: object`; name must match `[a-zA-Z][a-zA-Z0-9_]{0,63}`; no
   collision with a builtin.
4. **In-process import** into generation N+1.
5. **Atomic swap** of `ManifestRef`.

Any failure: manifest unchanged, `ext.failed` emitted with the traceback, and —
critically — **the error is returned to the model** if the reload came from a
tool call. The agent writes a tool, sees the syntax error, fixes it, reloads
again. That feedback loop is what makes self-extension actually work rather than
merely exist.

### 6.5 The money path: end-to-end

The scenario that has to work, in one turn, no restart:

```
iter 1  user: "you have no tool for querying our metrics API. build one and use it."
        model -> Read(.nexus/tools/_template.py)          [learns the contract]

iter 2  model -> Write(.nexus/tools/metrics_query.py)
                 SPEC = ToolSpec(name="MetricsQuery", bundle="ext",
                                 mutates=False, input_schema={...})
                 async def run(args, ctx): ...

iter 3  model -> ReloadExtensions()
        quarantine: ast ok -> subprocess import ok -> spec valid -> no collision
        manifest generation 7 -> 8
        ToolResult: "+1 tool: MetricsQuery (gen 8). 0 failed."

iter 4  assemble() reads manifest gen 8 -> tool schemas now include MetricsQuery
        model -> MetricsQuery(query="p99 latency, 24h")
        gate: bundle `ext`, mode=ask -> permission.requested -> user allows
        ToolResult: {...}

iter 5  model -> "p99 is 412ms. I also added a MetricsQuery tool; it persists
                  in .nexus/tools/ for future sessions."
```

Nothing here is special-cased. It falls out of `manifest.get()` being inside the
loop and `ToolManager` reading its registry from the manifest.

### 6.6 What deliberately still needs a restart

Honesty about the boundary, since the requirement says "without reload":

- Core Nexus modules (`core/`, `model/message.py`, `runtime.py`). Making these
  live-reloadable was the rejected option; the cost is a permanent tax on core
  code and stale-object bugs that are miserable to diagnose.
- The Python interpreter's own dependency set (new pip installs).
- The `Manifest` struct shape itself.

Mitigation so this is a paper cut, not a wall:

- `nexus doctor --explain-reload` states exactly what is hot and what is not.
- If the agent edits Nexus's own source, a hook detects it and tells the agent
  plainly: "core source changed; a restart is required for this to take effect;
  workspace extensions in `.nexus/tools/` are hot."
- `runtime.restart_requested` event lets a supervising UI offer a one-key
  restart that resumes the same session from its log.
- Escape hatch: anything genuinely needed live can be *implemented* as an
  extension rather than as core. The tool/provider/skill/hook interfaces are
  wide enough that this is usually possible. **If a capability keeps wanting to
  live in core and be hot, that is the signal the interface is too narrow** —
  widen the interface rather than making core reloadable.

---

## 7. Configuration

Layered, in increasing precedence: **built-in defaults -> `~/.nexus/config.toml`
-> `<workspace>/nexus.toml` -> `NEXUS_*` env -> CLI flags -> per-session
overrides.** Each layer is a partial document; merge is deep for tables,
replace for scalars, and `+=`-style append for the permission lists (so a
workspace can add an allow rule without restating the user's).

Validated on load with `msgspec`, reread at turn start (today's discipline,
kept), unknown keys still hard errors.

```toml
config_version = 2

[agent]
profile = "coding"
instructions_file = "SOUL.md"
memory_file = "MEMORY.md"
max_iterations = 60
max_turn_seconds = 1800

[model]
default = "anthropic/claude-opus-5"
fast    = "anthropic/claude-haiku-4-5-20251001"
plan    = "anthropic/claude-opus-5"
fallback = ["openai/gpt-5", "ollama/qwen3:32b"]

[model.params]
temperature = 1.0
max_output_tokens = 32000
thinking_budget = 10000

[providers.anthropic]
api_key = "${env:ANTHROPIC_API_KEY}"
base_url = "https://api.anthropic.com"

[providers.openai]
api_key = "${env:OPENAI_API_KEY}"
base_url = "https://api.openai.com/v1"
api = "responses"                       # responses | chat

[providers.groq]
kind = "openai_compatible"              # <- no new code to add a vendor
base_url = "https://api.groq.com/openai/v1"
api_key = "${env:GROQ_API_KEY}"

[providers.ollama]
base_url = "http://localhost:11434"

[context]
max_tokens = 180000
safety_margin_tokens = 4000
compaction = "hybrid"                   # drop_oldest | evict_tool_results | summarize | hybrid
compact_at_fraction = 0.85

[context.limits]
memory = 8000
skills_index = 4000
environment = 2000
attachments = 20000

[permissions]
mode = "ask"
allow = ["Read(**)", "Glob(**)", "Grep(**)", "LS(**)"]
deny  = ["Bash(rm -rf /*)", "Read(**/.env)", "Read(~/.ssh/**)"]
write_roots = ["./"]
on_unattended = "deny"

[tools]
bash_timeout_s = 120
max_result_tokens = 25000
max_parallel = 8

[ext]
enabled = true
watch_interval_ms = 500
dirs = [".nexus/tools", "~/.nexus/tools"]
quarantine = true
max_file_bytes = 262144

[mcp]
enabled = true
connect_timeout_s = 20
restart_max = 5

[session]
store = "jsonl"
snapshot_every = 20

[telemetry]
log_level = "info"
log_file = ".nexus/logs/nexus.log"
redact = ["api_key", "token", "authorization", "password", "secret"]
```

Two things to call out:

- `providers.<name>.kind = "openai_compatible"` means **adding Groq, Together,
  OpenRouter, vLLM, LM Studio, or Fireworks is a config entry, not a code
  change.** That is most of the "connect to different model providers" goal
  delivered by one adapter.
- **Secrets never appear in config values.** Only `${env:VAR}` or
  `${keychain:service}` references, resolved at use time, never written to the
  session log, never rendered into context, and scrubbed from logs and events
  by the `telemetry.redact` list. `~/.nexus/credentials.json` is created `0600`
  and is the only file allowed to hold literal tokens.

---

## 8. Providers

One adapter per wire protocol, not per vendor.

| Adapter | Covers | Notes |
| --- | --- | --- |
| `anthropic.py` | Claude (Opus/Sonnet/Haiku/Fable) | Reference implementation. Messages API, `tool_use`/`tool_result`, extended thinking + signatures, `cache_control` breakpoints, `/v1/messages/count_tokens`. |
| `openai.py` | OpenAI + **every** OpenAI-compatible endpoint + Codex models | Both `responses` and `chat.completions` dialects behind one class, selected by config. `base_url` override is the extensibility lever. Streams tool args as partial JSON -> `ToolCallAccumulator`. |
| `gemini.py` | Gemini | `generateContent` streaming, `functionDeclarations`, `Part`-based content. Genuinely different shape — **this is the adapter that proves the IR**, so build it early (Phase 7a, not last). |
| `ollama.py` | Ollama, llama.cpp server | Local, offline, no key. Weak/absent tool support on many models -> `Capabilities.tools=False` per model, loop adapts. Also the honest integration test for capability degradation. |
| `copilot.py` | GitHub Copilot | Gated on assumption #1 (token exchange + licence terms). |
| `opencode.py` | OpenCode | Gated on assumption #2. Reuses its credential store / model catalogue; falls back to `SubprocessAgentProvider` if the surface is CLI-only. |
| `scripted.py` | tests | Replays recorded stream fixtures deterministically. No network, no keys. |
| `legacy_codex_cli.py` | today's behaviour | Kept working through Phase 6, deleted in Phase 7. |

### Shared infrastructure (the reason adapters stay small)

`model/http.py` owns: one pooled `httpx.AsyncClient`, SSE line framing,
retry with jittered exponential backoff on 429/5xx/timeouts honouring
`Retry-After`, per-provider concurrency limits, and request/response logging
with redaction. An adapter's job reduces to **schema translation in, stream
translation out** — typically 150-250 lines.

### Model routing (`model/router.py`)

`"anthropic/claude-opus-5"` -> `(provider="anthropic", model="claude-opus-5")`.
Bare `"claude-opus-5"` resolves via an alias table. Config aliases (`default`,
`fast`, `plan`) let skills and subagents say `model: fast` and stay portable.
`model.fallback` is tried on provider-level failure (not on refusal, and not
mid-stream) with `model.retrying` emitted so the switch is never invisible.

### Adding a provider — the extensibility check

The plan is only as good as this list being short:

1. Create `nexus/model/providers/<name>.py` (or drop it in `.nexus/providers/`
   for a hot-loaded one).
2. Implement `capabilities`, `stream`, optional `count_tokens`, `aclose`.
3. Add a `[providers.<name>]` config block.
4. Record one stream fixture; add it to the shared provider conformance suite.

No core changes. No loop changes. The conformance suite (section 9) is the
contract — if a new adapter passes it, the loop will drive it.

---

## 9. Testing

Current tests are good in character (real pipes, real locks, a real fake
executable, no network) and that character is worth preserving. The subject
changes completely.

| Layer | Approach |
| --- | --- |
| **Provider conformance** | One parametrized suite every adapter must pass, run against recorded fixtures: text-only, single tool call, parallel tool calls, streamed partial-JSON args, malformed args, thinking + signature replay, refusal, 429-then-success, mid-stream disconnect, usage accounting. **This is the most valuable test asset in the plan** — it is what makes "add a provider later" safe. |
| **Loop** | `ScriptedProvider` + fake tools. Covers: multi-iteration tool loops, `max_iterations`, permission deny mid-batch, tool timeout, cancellation at each await point, capability degradation (`tools=False`, `parallel=False`), crash-resume with a dangling `tool_use`. |
| **Context** | Property tests: assembly never exceeds budget; priority-0 parts never dropped; compaction is monotonic; identical inputs produce byte-identical prompts (cache stability); history stays a *contiguous* suffix. |
| **Permissions** | Table-driven rule matching. Adversarial paths: `../`, symlinks out of `write_roots`, unicode homoglyphs, `$HOME` expansion, null bytes, absolute paths disguised as relative. `deny` can never be overridden. |
| **Sessions** | Concurrent turn attempts (lock), crash mid-turn (partial log line), v1->v2 migration on real files, fork/replay fidelity, atomic-write interruption. |
| **Hot extension** | The load-bearing suite: add a tool mid-turn and call it in the next iteration; broken tool leaves the manifest untouched; in-flight generation-N call survives a swap to N+1; import-hang is killed by quarantine; 200 sequential reloads leak no modules and no memory; file deleted mid-turn. |
| **MCP** | Fake stdio server. Connect, list, call, `tools/list_changed`, server dies mid-call, server hangs, restart backoff, malicious tool description (injection wrapper present), tool-list cache invalidation. |
| **End-to-end** | Real providers, network-gated, `-m live`, excluded from CI. Fixtures are *recorded* from these runs. |

Test stack: `pytest` + `pytest-asyncio` (dev-only dependencies; the runtime
dependency list stays `httpx`, `msgspec`, `mcp`). `unittest` compatibility is
not preserved — the async and parametrization needs make it the wrong tool now,
and this is a rewrite.

CI: lint (`ruff`), types (`mypy --strict` on `core/`, `model/`, `tools/spec.py`
— the contract surface), full offline suite on Linux + macOS, Python 3.11-3.14.

---

## 10. Phasing

Each phase ends with a working harness on `main`. **The legacy Codex CLI adapter
stays functional until Phase 7**, so there is never a window where the repo is
broken. Estimates assume one engineer working with an agent.

### Phase 0 — Foundations (~3 days)

Contracts and plumbing only; no behaviour change.

Build: `errors.py`, `events.py` (new envelope), `config/` (layered, v2 schema),
`core/bus.py`, `core/registry.py`, `core/watch.py`, `core/cancel.py`,
`model/message.py`, `model/request.py`, `model/stream.py`,
`model/capabilities.py`, `model/provider.py`, `model/tokenizer.py`.
Add `httpx`/`msgspec` + pytest. Delete the stray `.suo/` directory.

Wrap the existing `CodexProvider` to satisfy the new `Provider` protocol
(synthesizing `Message`/`StreamEvent` from its text output) so `nexus run`
keeps working unchanged.

Exit: new contracts importable and unit-tested; `nexus run` and `nexus chat`
behave exactly as before; config v1 files still load via a shim.

### Phase 1 — Own the loop (~5 days)

Build: `core/turn.py`, `core/loop.py`, `session/` (log store, lock, manager,
migrate), `model/http.py`, `model/providers/anthropic.py`,
`model/providers/scripted.py`, `model/router.py`, minimal `context/manager.py`
(parts 1,2,7,9,10 — no tools yet), `runtime.py`.

Exit: a real multi-turn conversation against Anthropic with no tools, driven by
Nexus's own loop. v1 sessions migrate. Loop tests pass against `ScriptedProvider`.
**This is the phase that removes the Codex dependency from the critical path.**

### Phase 2 — Tools and permissions (~6 days)

Build: `tools/spec.py`, `tools/manager.py`, `tools/permissions.py`,
`tools/bundles.py`, `tools/builtin/` (Read, Write, Edit, MultiEdit, Glob, Grep,
LS, Bash, BashOutput, KillShell, TodoWrite). Concurrency planning. The
permission-request event + resolve API. CLI approval prompt.

Exit: Nexus edits files and runs commands on its own. Full adversarial
permission suite green. Feature parity with today's Codex-backed behaviour,
now under Nexus's control.

### Phase 3 — Context and sessions, properly (~4 days)

Build: `context/parts.py`, `context/budget.py`, `context/compact.py`,
`context/cache.py`; real token counting with caching; session fork/replay;
snapshots; `tool_result` eviction with `context_note`.

Exit: a 200-message session runs without overflow; prompt caching measurably
cuts input cost (record before/after numbers in the PR); compaction is
reproducible from the log.

### Phase 4 — Self-extension (~6 days)

The centrepiece. Build: `ext/manifest.py`, `ext/quarantine.py`,
`ext/manager.py`, `tools/loader.py`, `skills/`, `tools/builtin/skill.py`,
`tools/builtin/meta.py` (ReloadExtensions, ListExtensions, WriteTool), watcher
wiring, `.nexus/tools/_template.py`.

Exit: **the section 6.5 walkthrough runs green as an automated test.** Broken
extensions cannot break a turn. 200 reload cycles leak nothing.

### Phase 5 — MCP (~4 days)

Build: `mcp/client.py`, `mcp/manager.py`, `mcp/bridge.py`; hot add/remove;
tool-list caching; injection wrapper; failure isolation.

Exit: real servers (filesystem, github) work; killing a server degrades the
turn instead of failing it; editing `mcp.json` takes effect live.

### Phase 6 — Subagents and hooks (~4 days)

Build: `agents/manager.py`, `agents/runner.py`, `tools/builtin/task.py`,
`hooks/`. Depth/budget guards. Nested event re-emission.

Exit: parallel subagents run with isolated tool sets; a `PreToolUse` hook blocks
a write and the model sees why; event tree renders in the CLI.

### Phase 7 — Provider breadth (~6 days)

7a: `gemini.py` first — the different-shaped API that validates the IR. Fix
whatever it breaks in `model/message.py` *before* more adapters calcify the design.
7b: `openai.py` (responses + chat + compatible endpoints + Codex models),
`ollama.py`.
7c: `copilot.py`, `opencode.py` — after resolving assumptions #1 and #2.
7d: **delete `legacy_codex_cli.py`** and the fake-Codex tests.

Exit: conformance suite green for every adapter; a documented mid-session
provider switch with visible `context.degraded` events; `nexus.toml` alone adds
an OpenAI-compatible vendor.

### Phase 8 — Surfaces and polish (~5 days)

Build: CLI rewrite (`ui/cli.py`, `ui/render.py`, `ui/jsonl.py`), slash commands,
`nexus doctor` (config, providers, MCP, extensions, `--explain-reload`),
`nexus sessions` (list/fork/replay/export), `nexus ext` (list/validate/trash),
docs: README rewrite, `ARCHITECTURE.md`, `EXTENDING.md`, a rewritten `SOUL.md`.

Exit: a new user reaches a working multi-provider harness from a clean checkout
using only the README.

**Total: ~43 working days.** Phases 0-4 (~24 days) deliver the requested
architecture; 5-8 deliver breadth and polish. Phases 5 and 6 are independent of
each other and of 7 — they can reorder or parallelize.

---

## 11. Risks

| Risk | Why it bites | Mitigation |
| --- | --- | --- |
| **Rewriting the loop loses Codex's accumulated tool quality** | Codex's Read/Edit/Bash handle encodings, huge files, partial-line edits, interactive prompts, and ANSI. Naive reimplementations regress on all of it. | Treat `tools/builtin/` as a first-class deliverable with its own test corpus (binary files, CRLF, 100 MB files, unicode, symlinks, no-trailing-newline). Budget real time in Phase 2 and don't let it be "the easy part". |
| **Manifest swap races** | A turn reading a half-built manifest, or an in-flight tool call whose module got dropped. | Immutable `Manifest` + single atomic ref; one read per iteration; generation refcounting before dropping `sys.modules` entries. Dedicated concurrency tests in Phase 4. |
| **Hot-loaded code is arbitrary code** | Quarantine catches syntax and import crashes, not malice. The permission engine gates *calls*, not *loading*. | State the boundary plainly: extension files are trusted code, exactly as `nexus.toml` and `SOUL.md` are today. Gate `ext` bundle tools behind `ask` by default; log every load with a content hash; `.nexus/trash/` retains removed versions; `ext.enabled = false` for hostile contexts. |
| **Cross-provider history fidelity** | A session started on Claude with thinking blocks, continued on Gemini, may produce invalid requests. | Explicit per-adapter `Degradation` policy, `context.degraded` events, a conformance test for every migration pair, and a config switch to forbid mid-session provider changes. |
| **Context/token accounting drift** | Heuristic counts diverge from real tokenizers; a 10% error means overflow errors or wasted window. | Provider `count_tokens` where available, cached; calibrated heuristic elsewhere; `safety_margin_tokens`; and treat an overflow *rejection from the provider* as a recoverable event that triggers compaction and one retry. |
| **Prompt injection via tool and MCP output** | Tool results are untrusted text placed in context, and the harness has Bash and file writes. | Delimiter-wrapped untrusted content with a standing no-authority instruction; permission engine as the real backstop (a prompt cannot grant itself a denied rule); `deny` rules are absolute; hooks for organization policy. |
| **Scope creep into a framework** | Every manager invites abstraction. Nexus's value is being small and legible. | Hard budget: **core (`core/` + `model/` + `tools/spec.py`) stays under 2,500 lines.** Reviewed each phase. Anything that wants to be core and hot (section 6.6) is a signal to widen an interface, not to add a layer. |
| **The plan itself is large** | 43 days is long enough to lose the thread. | Every phase ends shippable on `main`; the legacy adapter keeps the repo usable until Phase 7; phases 5/6/7 are independently reorderable. |

---

## 12. What "done" means

The harness is finished when all of these are true:

1. `nexus run "..."` completes a tool-using turn against **Anthropic, an
   OpenAI-compatible endpoint, Gemini, and a local Ollama model**, driven by
   Nexus's own loop, with the Codex CLI not installed.
2. Adding an OpenAI-compatible vendor requires **only** a `nexus.toml` block.
3. Adding a new wire protocol requires **only** a new file in
   `model/providers/` that passes the conformance suite.
4. The section 6.5 walkthrough — write a tool, reload, call it, same turn, no
   restart — passes as an automated test.
5. A broken extension, a dead MCP server, a hung tool, and a 429 storm each
   degrade the turn with a visible event instead of crashing the harness.
6. `permissions.deny` cannot be overridden by the model, by a session grant, or
   by any path trick in the adversarial suite.
7. A 200-message session runs to completion with compaction and prompt caching,
   and the full original history remains reconstructible from the session log.
8. Switching `profile = "research"` produces a harness with no shell or
   file-write tools, and nothing in `core/` needed to change to allow it.
9. Every state change a UI could render is an event; no UI polls a manager.
10. Core stays under 2,500 lines.

---

## 13. Immediate next steps

1. Resolve the three open assumptions in section 0 (Copilot licence terms and
   endpoint, OpenCode integration surface, Codex model access). Each is a
   ~30-minute investigation and all three only affect Phase 7c.
2. **Rewrite `SOUL.md` first.** Its current text forbids skills, MCP,
   additional providers, and plugin discovery. Until it changes, an agent
   working in this repo will argue against this plan.
3. Start Phase 0. The first commit is `model/message.py` — the IR is the
   decision everything else inherits, and it is the one worth arguing about
   before any code depends on it.
