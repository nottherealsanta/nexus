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
deny  = ["Bash(rm -rf /*)", "Read(**/.env)"]
write_roots = ["./"]
read_denyroots = ["~/.ssh"]          # home paths belong here (roots expand '~')
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

---

## 14. Amendment: the host layer and multiple surfaces

Added after Phase 3 shipped. This section is **append-only by design**: it does
not rewrite sections 1-13, it amends them by reference. Where this section and
an earlier one disagree, this section wins.

### 14.0 What this amends

| Section | Amendment |
| --- | --- |
| §1 Non-goals | Non-goal *"No web UI"* is **withdrawn**. A web surface is in scope; only its *frontend* is deferred (§14.12). |
| §2.1 Layering | Two layers inserted: `view/` just above L0, `host/` between L4 and L5. |
| §2.2 File layout | Adds `nexus/host/`, `nexus/view/`, restructures `nexus/ui/`. |
| §3.5 Event catalogue | Adds the `input.*`, `presence.*`, and `daemon.*` groups (§14.10). |
| §5.1 SessionManager | `Session.send()` splits into `start_turn()` + `subscribe()` (§14.2). |
| §10 Phasing | Inserts **Phase 3.5** before Phase 4; rewrites Phase 8 as 8a-8c (§14.11). |
| §11 Risks | Adds three risks (§14.13). |
| §12 Done | Adds criteria 11-13 (§14.14). |

### 14.1 Decisions added

Settled before writing this section; treated as fixed the same way §0 is.

| Decision | Choice |
| --- | --- |
| Surfaces | Core is exposed through a transport-neutral **facade**. A prompt_toolkit CLI and an HTTP surface are peers over it. Neither is privileged. |
| Process model | **Daemon always.** The daemon owns every `Runtime`. The CLI is a pure client with no in-process fallback. |
| Concurrency | Multiple sessions run turns **concurrently in one daemon**. A session's turn outlives every view of it. |
| Views | **Single user, many views.** No multi-user identity, no per-user auth, no collaborative editing. Presence is a subscriber *count*. |
| UI boundary | A UI may import `nexus.host`, `nexus.view`, `nexus.events` — nothing else. Enforced by a layering test, not by discipline. |
| UI extension | Declarative-first, mirroring §0's self-extension rule: config, data files, and an above-the-facade renderer registry. No UI plugin ever receives a `Runtime`. |
| Web frontend | **Deferred.** The protocol and the HTTP/SSE transport ship; no HTML does. |

### 14.2 Why the present boundary is insufficient

Phase 0-3 got the hard part right: `Session.send()` yields `Event` envelopes,
everything is msgspec-serializable, and `nexus/ui/native.py` is already a real
adapter that imports only `Event` and `Decision`. But the stream is
**turn-scoped and single-consumer**, which blocks all three new requirements:

1. **One consumer exists.** `_Fanout` is constructed inside `send()` and dies
   with the turn. `core/bus.py` has a real multi-subscriber `Bus`; it is not
   wired to sessions. A second view cannot attach.
2. **The consumer owns the producer's lifetime.** Closing the generator cancels
   the turn — correct for a CLI, fatal for a background session or a browser
   tab.
3. **`resolve_permission()` is a direct method call.** Meaningless over a wire.

The fix is to split the primitive and keep `send()` as a wrapper over it:

```python
turn_id = await session.start_turn(content)     # runs to completion, unowned
async for ev in session.subscribe(from_seq=n):  # any number of watchers
```

Sessions are already an append-only JSONL log with `seq` monotonic per session,
so "catch up, then follow" is: read the log to `seq`, subscribe from `seq+1`.
**Resumable, replayable, multi-watcher streams fall out of `session/store.py`
as it already exists.** A reconnecting browser and a second terminal are the
same case, and so is `nexus replay`.

### 14.3 New layers

```
    L5  ui/            cli (prompt_toolkit), web         <- surfaces
        ------------------------------------------------
    L4½ host/          facade, protocol, supervisor, transports
        ------------------------------------------------
    L4  runtime.py     Runtime: owns managers, wiring, reload
        ------------------------------------------------
    L3  managers       session/ context/ tools/ skills/ mcp/ agents/ hooks/ ext/
        ------------------------------------------------
    L2  core/          loop, turn, bus, registry, watch
        ------------------------------------------------
    L1  model/         message IR, Provider protocol, adapters
        ------------------------------------------------
    L0½ view/          pure event reducer  (imports events.py ONLY)
        ------------------------------------------------
    L0  config, errors, events, util
```

`view/` sits low deliberately: it depends on nothing but the `Event` envelope,
so the daemon, the CLI, and any future frontend can all reduce the same stream
with the same code.

```
nexus/
  view/
    model.py                  ConversationView, TurnView, ToolCallView, ...
    reduce.py                 apply(state, event) -> state   (pure, sync)
    fold.py                   text.delta accumulation, dedup, ordering

  host/
    facade.py                 the complete verb list a surface may call
    protocol.py               msgspec Command/Response structs (the wire contract)
    supervisor.py             N concurrent sessions, turn scheduling, caps
    presence.py               subscriber counts -> attended derivation
    daemon.py                 lifecycle, socket, handshake, shutdown
    transports/
      uds.py                  local CLI <-> daemon (framed JSON)
      http_sse.py             HTTP commands + SSE event stream

  ui/
    cli/                      prompt_toolkit line-mode client   [extra: nexus[cli]]
      app.py  render.py  commands.py  keys.py  theme.py
    jsonl.py                  --json passthrough
```

`nexus/ui/native.py` is retired into `ui/cli/` at Phase 8b.

### 14.4 The facade

`host/facade.py` is the **only** surface API. Everything a UI can do is here;
anything not here, a UI cannot do.

```
commands      open_session(workspace, title?) -> id
              start_turn(session, content) -> turn_id
              enqueue(session, content) -> queued_id
              cancel(session, reason?)
              resolve_permission(session, request_id, decision)
              fork(session, at_seq?) -> id
              delete(session)
              reload_extensions()
queries       list_sessions() -> [SessionSummary]
              state(session, from_seq) -> (ConversationView, seq)
              list_tools() / list_models() / doctor()
subscription  subscribe(session, from_seq) -> AsyncIterator[Event]
```

`SessionSummary` is what makes background work legible and is a hard
requirement, not a convenience:

```python
class SessionSummary(Struct, frozen=True):
    id: str
    title: str                # auto-generated from the first message
    state: Literal["idle", "running", "awaiting_input", "awaiting_permission"]
    last_activity: float
    last_seq: int             # a view diffs this against what it has rendered
    viewers: int
```

Both surfaces render this identically, because both reduce the same view model.

### 14.5 Background sessions

The daemon runs many sessions at once. Three things this needs beyond
`start_turn`:

**A supervisor with a cap.** `host/supervisor.py` schedules turns across
sessions under `daemon.max_concurrent_turns`. Without it, five background
sessions x parallel tool calls x subagents is a fork bomb.

**A per-session unattended policy.** §5.3's `permissions.on_unattended` becomes
settable per session, so background work can be told to auto-deny and keep
going rather than block.

**Session-scoped shared state — a defect to fix first.** `JobRegistry` is *not*
session-scoped: `spawn()` takes no session id, `_jobs` is a flat dict, and
`Runtime` builds exactly one registry shared by every session
(`nexus/runtime.py:372`). With concurrent sessions, session A can
`BashOutput`/`KillShell` a job belonging to session B. `TodoStore` already does
this correctly (keyed by `session_id`); `JobRegistry` must match. The same audit
applies to MCP connections, the shared `httpx` client, and provider rate limits:
**state that was safe when only one session could be live is now shared mutable
state.** A concurrency test suite covering two sessions racing on each shared
manager is part of Phase 3.5's exit criteria.

The dominant failure mode of a background session is *silence* — it hits an
approval prompt and stops, and nobody notices for twenty minutes. That is why
`awaiting_permission` is a first-class value in `SessionSummary.state` and why
permission requests carry a timeout.

### 14.6 Many views of one session

Single user throughout. No identity model, no auth beyond the localhost token,
no shared draft buffers or cursors. "Multiplayer" here means **shared
observation and shared control of one session from several windows.**

**Presence replaces the `attended` flag.** Today `attended` is set once and
gates whether approvals are prompted for. If a view attaches and then
disconnects mid-turn, the session stays marked attended and blocks forever on
an approval nobody will answer. Attendance becomes derived:
`attended = viewers > 0`, recomputed on every subscribe/unsubscribe, and a drop
to zero applies the session's unattended policy to any pending request.

**Input while a turn runs.** The turn lease (`begin_turn` + flock) rejects a
concurrent send with `SessionBusy`. Add `enqueue(session, content)`: the input
is persisted, emits `input.queued`, and the loop consumes it at the next turn
boundary. One primitive serves both a second view and the single impatient user
typing while the agent works.

**Permission races.** First responder wins. `resolve_permission` already returns
`bool`, so a losing view simply gets `False` and re-renders from
`permission.resolved`.

**Optional `client_id`.** Events may carry the originating view's id for
attribution and echo handling. This is a debugging aid, not an identity model,
and nothing in the loop may branch on it.

### 14.7 The view reducer

Without this, every surface independently reinterprets forty event types and
they drift. `nexus/ui/native.py` already documents the trap: stream
`text.delta`, suppress the finalized `text` so nothing prints twice, track
terminal events. That logic belongs in exactly one place.

```python
def apply(state: ConversationView, event: Event) -> ConversationView: ...
```

Pure, synchronous, no I/O, importing only `nexus.events`. It produces a
renderable tree — turns, text blocks, thinking blocks, tool calls with live
status and results, pending permission requests, usage totals. The CLI renders
it to a terminal; the HTTP surface serializes it to JSON; a future frontend
renders it in a browser. Identical semantics by construction.

It is also testable with no terminal and no browser: feed a recorded session
log, assert the view model, keep golden snapshots. `nexus replay <session>`
is the same code path and becomes the primary UI regression harness.

### 14.8 Daemon and client

**The daemon always owns the Runtime. There is no in-process fallback.** One
code path, one set of semantics, and multiple views work by construction rather
than by a transport that only some surfaces use.

The consequence, stated plainly: `nexus run` in a CI script now depends on a
background process. That is acceptable only if the daemon is invisible when it
is working, so `host/daemon.py` owns:

- **auto-start** — the client starts a daemon if the socket is absent, then
  waits for readiness with a bounded timeout;
- **a deterministic socket path** — `~/.nexus/daemon/<hash-of-workspace>.sock`,
  so a workspace maps to exactly one daemon;
- **stale-socket cleanup** — a socket whose owning pid is gone is removed and
  replaced, not reported as an error;
- **a version handshake** — a client and daemon built from different versions
  must fail loudly on connect, never speak a half-understood protocol;
- **an idle shutdown policy** — exit after `daemon.idle_timeout` with no
  viewers *and* no running turns; never exit with a turn in flight;
- **`nexus daemon status|stop|logs`**.

Exit criterion for this piece: `nexus run "..."` from a clean machine with no
daemon running behaves exactly as it does today, including exit codes.

### 14.9 Transport and wire protocol

One protocol (`host/protocol.py`), two transports, same `Command` and `Event`
structs on both:

| Transport | Used by | Shape |
| --- | --- | --- |
| `uds.py` | CLI <-> daemon | length-framed JSON over a Unix socket |
| `http_sse.py` | HTTP surface | commands as POST, events as SSE |

SSE rather than WebSocket: commands are infrequent and events are the volume,
and SSE's `Last-Event-ID` maps **exactly** onto the session log's `seq`. The
resumability already present on disk becomes the resumability of the wire with
no extra machinery. WebSocket stays available if a future frontend needs
bidirectional low latency.

### 14.10 Events added to the §3.5 catalogue

```
input.queued     input.consumed    input.dropped
presence.joined  presence.left
daemon.started   daemon.stopping   daemon.session_scheduled  daemon.session_queued
```

The §3.5 rule is unchanged and now load-bearing across processes: **every state
change a UI could draw is an event; no UI polls the facade.** The queries in
§14.4 exist only so a late-joining view can establish a baseline before
subscribing.

### 14.11 UI encapsulation and customizability

**Encapsulation is enforced, not requested.** `tests/test_layering.py` already
walks the AST to assert `nexus/model/**` never imports `nexus.core` and above.
Extend that same machinery: anything under `nexus/ui/**` may import only
`nexus.host`, `nexus.view`, `nexus.events`, and the standard library. An import
of `nexus.runtime`, `nexus.core`, `nexus.tools`, `nexus.session`, or
`nexus.model` from a UI fails CI. Surfaces additionally ship as extras
(`nexus[cli]`, `nexus[web]`) so the dependency cannot run backwards either.

**Customizability is declarative**, mirroring §0's rule for tools:

| Mechanism | What it customizes |
| --- | --- |
| `[ui]` in `nexus.toml` | theme colours, compact vs. verbose tool rendering, which event types render, keybinding map |
| `.nexus/commands/*.md` | slash commands as data — same frontmatter shape as skills |
| renderer registry | a per-tool-name or per-event-type renderer, registered **above** the facade |

A bad renderer breaks a pane, never a turn. There is deliberately no UI plugin
API that receives a `Runtime`, a manager, or a tool.

**The CLI is line-mode**, not a full-screen application: prompt_toolkit owns the
input line, `patch_stdout()` keeps streaming output from scrambling it, and
terminal scrollback and copy-paste keep working. `prompt_async` also removes the
`asyncio.to_thread(input)` workaround the current approver relies on. A
full-screen TUI, if ever wanted, is a separate surface over the same facade.

Basic elements, identical on every surface because they render one view model:
session switcher with state and unread marker; transcript with streaming text,
collapsible thinking, and tool calls showing name, argument preview, status, and
expandable result; approval prompt with the four `Decision` values; input with
history, multi-line, and cancel; status bar with model, usage, turn state, and
viewer count; slash commands `/new /sessions /model /tools /cancel /fork
/export`.

### 14.12 Security

The daemon exposes an authenticated local endpoint that can run `Bash` on the
host. This is a permanent property of the design, not a Phase 8 to-do:

- bind `127.0.0.1` only; the Unix socket is `0600`;
- a token generated at daemon start, required by the HTTP transport, never
  logged;
- `Origin` checked on every HTTP request;
- credentials never traverse the facade in either direction;
- `deny` rules (§5.3) remain absolute and are evaluated daemon-side, never
  client-side.

The web *frontend* is deferred; the transport and protocol are not, so these
constraints are built in Phase 8c rather than retrofitted.

### 14.13 Revised phasing

**Phase 3.5 — Session surface (~3 days). Runs before Phase 4.**

Core only, no UI. It changes `session/`, which Phases 4-7 all build on, so
doing it later means touching them twice.

Build: `Session.start_turn()` / `Session.subscribe(from_seq)` with `send()`
retained as a wrapper; wire `core/bus.py` to sessions so a turn survives with
zero subscribers; the input queue (`enqueue`, `input.*` events); presence
counting and derived attendance; **session-scope `JobRegistry`**; the shared
mutable state audit from §14.5.

Exit: a turn started with no subscriber runs to completion and is fully
recoverable by a late subscriber from `seq=0`; two sessions run concurrent
turns with no shell-job cross-talk; a subscriber disconnecting mid-turn applies
the unattended policy instead of hanging.

**Phase 8 replaces §10's Phase 8 entirely.**

| Phase | Build | Days |
| --- | --- | --- |
| **8a** | `view/` reducer + golden-log tests; `host/facade.py`, `host/protocol.py`, `host/supervisor.py`, `host/presence.py`; the UI layering test; `nexus replay` | ~4 |
| **8b** | `host/daemon.py` + `transports/uds.py`; auto-start, handshake, stale-socket cleanup, idle shutdown; `nexus daemon status\|stop\|logs`; prompt_toolkit CLI over it; retire `ui/native.py` | ~5 |
| **8c** | `transports/http_sse.py` + token auth + Origin checks; `nexus doctor`, `nexus sessions`, `nexus ext`; README / `ARCHITECTURE.md` / `EXTENDING.md` / `SOUL.md` rewrite | ~4 |

~13 days against the 5 originally budgeted for Phase 8, plus Phase 3.5's 3.
**Revised total: ~54 working days.** No web frontend is included; when one is
built it is additive and touches nothing below `ui/`.

### 14.14 Risks added to §11

| Risk | Why it bites | Mitigation |
| --- | --- | --- |
| **The daemon becomes a single point of failure for scripting** | With no in-process fallback, a stale socket or a crashed daemon breaks `nexus run` in CI, where today nothing can. | Auto-start with bounded readiness wait; stale-socket reclaim; version handshake that fails loudly; an exit criterion that a clean machine with no daemon behaves exactly as today. |
| **Concurrent sessions expose shared mutable state** | Managers written when one session could be live are now reached from several at once — `JobRegistry` is already wrong (§14.5). | Session-scope every registry; a two-session race suite per shared manager as a Phase 3.5 exit gate. |
| **Surfaces drift apart** | Two renderers independently interpreting 40 event types diverge quietly, and bugs get fixed once. | One pure `view/` reducer that both import; golden view-model snapshots from recorded logs; the AST layering test making the boundary a CI failure rather than a convention. |

The §11 core budget is amended: **the 2,500-line cap covers `core/` + `model/`
+ `tools/spec.py` only.** `host/`, `view/`, and `ui/` are explicitly outside it
and get their own budget of 2,000 lines, reviewed the same way.

### 14.15 Criteria added to §12

11. Two views of one live session — a terminal and an HTTP client — render
    identical state, and either can approve a permission request.
12. A turn started from a view that then disconnects runs to completion, and a
    view attaching afterwards reconstructs the full turn from the session log.
13. No module under `nexus/ui/**` imports anything outside
    `{nexus.host, nexus.view, nexus.events, stdlib}`, proven by a test.

### 14.16 Explicitly deferred

**A Jupyter/IPython kernel as a default tool** — a persistent execution
namespace the model can hold state in, with tools callable from inside it
("code mode"). Discussed and deliberately parked until the harness in §§0-13
plus this amendment is working. It touches `tools/`, `context/` (a part
rendering the live namespace), and the permission engine (arbitrary Python
bypasses `write_roots`), so it wants a stable core underneath it. Revisit after
Phase 8c.

---

## 15. Amendment: model registry, tiers, and subagents as a tool

Second append-only amendment, same rules as §14: sections 1-14 are not
rewritten, only amended by reference. Where this section disagrees with an
earlier one, this section wins.

### 15.0 What this amends

| Section | Amendment |
| --- | --- |
| §0 Assumptions | Adds assumption #4: models.dev licence and redistribution terms (§15.12). |
| §2.2 File layout | Adds `model/registry.py`, `model/tiers.py`, `model/data/`. |
| §3.5 Events | Adds `registry.*` and widens `agent.*` (§15.10). |
| §5.6 AgentManager | Extended, not replaced: adds ad-hoc spawning, the three seeded roles, and parent-bounding (§§15.6-15.8). |
| §8 Model routing | The hand-maintained alias table is replaced by the registry + tier resolution (§§15.3-15.4). |
| §10 Phasing | Inserts **Phase 5.5**; Phase 6 grows (§15.11). |
| §11 Risks | Adds two risks (§15.12). |

### 15.1 Decisions added

| Decision | Choice |
| --- | --- |
| Catalogue source | **models.dev** (`https://models.dev/api.json`). Fetched on first use, cached with a TTL, with a small vendored snapshot as the offline fallback. |
| Tier assignment | **Curated defaults, cost-based fallback, user override.** Shipped map wins for known models; blended cost classifies the rest; `[models.tiers]` overrides both. |
| Capabilities | **The registry is authoritative.** No per-adapter override table. A provider rejection that contradicts the registry degrades the turn and is logged as a data defect (§15.5). |
| Subagent invocation | Two modes: a **named type** (`general`, `explore`, `planner`, or any `.nexus/agents/<name>.md`) or an **ad-hoc** spawn with a task prompt and an explicit tool list. |
| Subagent authority | **A child can never exceed its parent.** Tool lists intersect, permissions inherit, `deny` stays absolute, tier and fan-out are capped by config. |
| Role definitions | The three roles are **seeded files** in `.nexus/agents/`, editable and deletable like any other extension. |

### 15.2 What the catalogue actually contains

Measured, not assumed (fetched 2026-09-22):

- **223 providers, 7,997 models, 4.8 MB** of JSON.
- Per model: `cost{input,output,cache_read,cache_write}` in $/Mtok,
  `limit{context,output}`, `tool_call`, `reasoning` + `reasoning_options`,
  `structured_output`, `temperature`, `attachment`,
  `modalities{input,output}`, `open_weights`, `family`, `knowledge`,
  `release_date`, `last_updated`.
- Per provider: `env` (credential env var names), `npm`, `doc`, `name`.

Three properties drive the design:

1. **`env` is the filter that makes 7,997 tractable.** Ingest keeps only
   providers whose env vars are present or that appear in a `[providers.*]`
   config block. In practice that is single digits of providers.
2. **The catalogue is full of aliases and re-listings.** `openai/o1-pro`,
   `openrouter/openai/o1-pro`, and `nano-gpt/openai/o1-pro` are one model.
   Ingest canonicalizes and prefers the direct provider, keeping the others as
   `aliases` so a user-typed aggregator id still resolves.
3. **It is not only chat models.** `text-embedding-3-small` and `gpt-image-2`
   are in there. Ingest keeps only entries whose `modalities.output` includes
   `text`; everything else is dropped.

421 models carry no cost data (local and open-weight). They classify as `low`
unless the curated map or user config says otherwise.

### 15.3 The registry (`model/registry.py`)

L1, alongside the other model contracts. It is data and lookup only — it
performs no I/O during a turn.

```python
class ModelInfo(Struct, frozen=True):
    provider: str                 # nexus provider id, mapped from models.dev
    id: str                       # canonical model id
    name: str
    family: str | None
    aliases: tuple[str, ...]      # aggregator re-listings that resolve here
    context: int
    max_output: int
    tool_call: bool
    reasoning: bool
    structured_output: bool
    temperature: bool
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    cost: Cost | None             # None for local/open-weight
    tier: Literal["high", "medium", "low"] | str
    source: Literal["catalogue", "config", "builtin"]
```

**Acquisition.** Fetch on first use into `.nexus/cache/models.dev.json`, TTL
default 7 days. On a cache miss with no network, fall back to
`nexus/model/data/models.min.json` — a filtered snapshot of the major direct
providers, refreshed at release time, kilobytes rather than megabytes. Never
fetched during tests: the conformance suite and every unit test run against a
fixture registry. `nexus models refresh` forces it.

**Provider mapping.** A models.dev provider id maps to a Nexus adapter
(§8's one-adapter-per-wire-protocol rule), not one-to-one. `npm`
(`@ai-sdk/anthropic`, `@ai-sdk/openai`, `@ai-sdk/google`) is the strongest
available signal and seeds a small explicit mapping table; anything unmapped
falls back to the OpenAI-compatible adapter with its `base_url` taken from
config. An unmappable provider is listed but not selectable, with the reason
shown by `nexus doctor`.

**The registry does not carry credentials or base URLs.** It is descriptive
data. Endpoints and keys stay in `[providers.*]` and `credentials.json`, which
keeps a third-party catalogue from being able to redirect a request.

### 15.4 Tiers (`model/tiers.py`)

Three tiers ship: `high` (frontier, expensive), `medium` (the workhorse), `low`
(fast and cheap, for quick mechanical work). The table is open — a user may add
names — but the three built-ins always resolve.

Resolution order, first hit wins:

1. `[models.tiers]` in config — an explicit user pin.
2. The curated default map shipped with Nexus.
3. Blended-cost fallback: `blended = cost.input + cost.output / 4`
   (agentic traffic is input-heavy), then `low <= 2.5 < medium <= 10 < high`.
4. No cost data -> `low`.

The fallback is calibrated against real pricing — Haiku 4.5 lands at 2.25,
Sonnet at 4.5-6.75, Opus at 11.25, Fable at 22.5 — and it is deliberately only
a fallback. It misclassifies flagships that happen to be cheap (`gpt-5.6` at
9.0 reads as medium), which is exactly what the curated map is for.

```toml
[models]
default = "medium"                 # the main session's model, too
refresh_ttl_days = 7

[models.tiers]
high   = ["anthropic/claude-opus-5", "openai/gpt-5.6"]
medium = ["anthropic/claude-sonnet-5"]
low    = ["anthropic/claude-haiku-4-5"]
```

A tier name is usable **anywhere a model string is**: `nexus.toml`'s
`models.default`, a skill's `model:`, an agent definition's `model:`, and
`Task(model="low")`. This replaces §8's ad-hoc `default`/`fast`/`plan` aliases;
those become tier names or config pins.

### 15.5 Capabilities from the registry

`model/capabilities.py`'s `Capabilities` is populated from `ModelInfo`:
`tool_call`, `context`, `max_output`, `reasoning`, `structured_output`,
`temperature`, and the modality lists. The registry is authoritative and there
is no per-adapter override table.

The one safeguard, which does not contradict that: when the registry claims a
capability and the provider rejects the request for it, the loop treats the
rejection as a **recoverable degradation** — emit `context.degraded` with the
specific mismatch, retry once without the offending feature, and write the
mismatch to the log as a catalogue defect. A wrong upstream entry costs one
retry and produces an actionable report; it does not crash a turn and it does
not silently succeed. `nexus doctor` surfaces accumulated mismatches so bad
entries can be fixed upstream.

### 15.6 `Task` — subagents as a tool

§5.6's `Task` is extended rather than replaced. One tool, not three: a single
permission key, one schema in context.

```python
Task(
    prompt: str,                    # required: the task
    subagent_type: str = "general", # a seeded role or any .nexus/agents/<name>
    tools: list[str] | None = None, # ad-hoc: narrows the role's set
    model: str | None = None,       # tier name, "provider/model", or bare id
    description: str | None = None, # short label for the UI tree
)
```

Two modes, as specified:

- **Named type.** `Task(subagent_type="explore", prompt="...")` — system prompt,
  tool set, and model all come from the definition file.
- **Ad-hoc.** `Task(prompt="...", tools=["Read","Grep"], model="low")` — a
  throwaway agent with exactly the listed tools. `subagent_type` defaults to
  `general`, whose system prompt is written to work without task-specific
  framing.

The two compose: a named type with `tools` narrows that role for one call.

`permission_key` returns `"<subagent_type>:<tier>"`, so the rule grammar in
§5.3 can express real policy without new syntax:

```toml
deny  = ["Task(*:high)"]            # never spawn a frontier-tier subagent
allow = ["Task(explore:*)", "Task(*:low)"]
```

Everything else from §5.6 is unchanged: nested Runtime, child session at
`<parent>/sub/<n>` logged under the parent so the tree replays, events
re-emitted on the parent bus with an `agent` field, final report returned as a
`ToolResult`.

### 15.7 The three seeded roles

Written into `.nexus/agents/` on first run, then ordinary hot-loaded extensions
— editable, forkable, deletable. A deleted file re-seeds only in a fresh
workspace. Shadowing precedence is §2.3's: workspace > user > builtin.

| Role | Tools | Model | Purpose |
| --- | --- | --- | --- |
| `general` | inherits the parent's set | `medium` | Catch-all delegation. The only role that can write. |
| `explore` | `fs` bundle minus every mutating tool; `Grep`, `Glob`, `Read`, `LS` | `low` | Broad fan-out search. Returns findings, not file dumps. |
| `planner` | same read-only set | `high` | Designs an approach and returns a plan. Cannot execute it. |

`explore` and `planner` have **no write path at all** — not `Write`, `Edit`,
`MultiEdit`, or `Bash`. This is enforced structurally by the same profile
machinery as §5.3's `research` profile, not by system-prompt instruction.

The tier assignments are the point of the whole feature: fan out ten `low`-tier
explorers cheaply, spend `high` on the single planning call, keep `medium` for
the work.

### 15.8 A subagent can never exceed its parent

Four rules, all enforced in `agents/runner.py` before the child Runtime is
constructed:

1. **Tools intersect.** The child's tool set is
   `parent_tools & role_tools & requested_tools`. A name the parent lacks is
   dropped, and the drop is reported in the `ToolResult` so the model learns
   rather than silently getting less than it asked for.
2. **Permissions inherit.** The parent's rules, session grants, `write_roots`,
   and `read_denyroots` all apply to the child. `deny` remains absolute and is
   evaluated in the child's own gate.
3. **Tier is capped.** `agents.max_tier` (default `medium`) bounds what the
   model may request. A request above the cap is clamped, not refused, and the
   clamp is reported.
4. **Fan-out is capped.** `agents.max_concurrent` (default 4) plus §5.6's
   existing `max_depth` (3) and the aggregate token budget across the tree.

Consequence worth stating plainly: a session running the `research` profile
cannot spawn a child that writes files. The profile boundary in §5.3 stays a
hard boundary rather than something a `Task` call can step around.

```toml
[agents]
max_tier = "medium"
max_concurrent = 4
max_depth = 3
default_type = "general"
```

### 15.9 Config summary

```toml
[models]
default          = "medium"
refresh_ttl_days = 7
catalogue_url    = "https://models.dev/api.json"
offline          = false           # true: never fetch, snapshot only

[models.tiers]
high   = [...]
medium = [...]
low    = [...]
```

### 15.10 Events and CLI

Added to the §3.5 catalogue:

```
registry.refreshed   registry.stale   registry.failed   registry.mismatch
agent.clamped        (tier or tool set narrowed from what was requested)
```

`agent.clamped` exists so a UI can show that the model asked for `high` and got
`medium` — a silent clamp would be a debugging trap.

CLI, added to §14's Phase 8c surface set: `nexus models list [--tier] [--provider]`,
`nexus models show <id>`, `nexus models refresh`, `nexus models tiers`,
and `nexus agents list`.

### 15.11 Phasing

**Phase 5.5 — Model registry (~3 days). Before Phase 6.**

Build: `model/registry.py`, `model/tiers.py`, the ingest filter
(env-gated, canonicalized, text-output-only), the provider mapping table,
`model/data/models.min.json`, TTL caching in `.nexus/cache/`, `Capabilities`
population, and the degradation path in §15.5. Depends only on `model/`, so it
slots anywhere before Phase 6 needs tiers.

Exit: `nexus models list` shows only reachable models; a tier name resolves
everywhere a model string is accepted; the whole suite runs with no network
against a fixture registry; a deliberately wrong fixture capability produces
one retry, a `registry.mismatch`, and a completed turn.

**Phase 6 grows by ~2 days** for the `Task` extensions: ad-hoc spawning, the
three seeded role files, the four bounding rules, `agent.clamped`, and an
adversarial suite proving a `research`-profile parent cannot obtain a writing
child.

**Revised total: ~59 working days** (from §14's ~54).

### 15.12 Risks added to §11

| Risk | Why it bites | Mitigation |
| --- | --- | --- |
| **A third-party catalogue is authoritative over capability gating** | A wrong `tool_call` or `context` upstream misconfigures every turn on that model, and by decision there is no local override table. | The §15.5 degradation path: one retry without the offending feature, `registry.mismatch` emitted and logged, `nexus doctor` reporting accumulated defects, and `models.offline = true` pinning to the vetted snapshot for anyone who wants determinism. |
| **Cheap subagents make expensive fan-out easy** | `Task` is now trivial to call with a tier, so the model can spawn many agents without the user seeing the cost until the bill. | `agents.max_tier` and `max_concurrent` default conservatively; the aggregate tree token budget from §5.6 is enforced, not advisory; `Task(*:high)` is expressible as a `deny` rule; per-turn cost is surfaced in the status bar (§14.11). |

### 15.13 Open assumption #4 (extends §0)

**models.dev licence and redistribution terms.** The plan assumes the catalogue
may be fetched at runtime *and* that a filtered subset may be redistributed
inside the Nexus package as the offline fallback. Verify the project's licence
and any attribution requirement before Phase 5.5 ships. If redistribution is
not permitted, the vendored snapshot is dropped and `models.offline = true`
instead requires a one-time `nexus models refresh`; nothing else in this
section depends on it.

---

## 16. Amendment: as-built status ledger (Phase 0-8)

Third append-only amendment, same rules as §14 and §15: **sections 1-15 are not
rewritten.** This section records what is *actually built and verified*, not what
is required. Where it appears to conflict with a requirement section, the
requirement section still states the intent; this section only reports progress
against it. Nothing here relaxes a requirement.

Recorded 2026-09-23. Base commit `fc26db7` (`Remove obsolete test files for
harness, legacy codex provider, native CLI, and provider tests`) plus the
**uncommitted** Phase 8 worktree. Evidence: direct source inspection, the
**regenerated** (also uncommitted) closeout report
`tests/fixtures/reports/phase3_exit_baseline.txt`, a full offline suite run by
this ledger's author (not taken on faith), the real-subprocess HTTP+UDS daemon
tests, and a clean-workspace CLI smoke.

### 16.1 Committed vs. uncommitted

**Committed through `2a159fb` — Phases 0-7:**

| Commit | Content |
| --- | --- |
| `23ea874` | Phase 0 foundations |
| `39e1c89` | Phase 1 owned loop |
| `db1c3d3` | Phase 2 tools and permissions |
| `9ad195a` | Phase 3 context and sessions |
| `75caf77` | docs: host-layer amendment (§14) |
| `6cad851` | docs: model-registry amendment (§15) |
| `2cf74d6` | Phase 3.5 detached sessions + Phase 4 hot extensions |
| `e7a905f` | Phase 5 MCP + Phase 5.5 model registry/tiers |
| `da00543` | Phase 6 subagents and hooks |
| `2a159fb` | Phase 7 provider breadth |

**Uncommitted in the worktree — Phase 8 (not yet committed):**

- **Staged deletions (legacy cutover):** `nexus/agent.py`, `nexus/provider.py`,
  `nexus/store.py`, `nexus/ui/native.py`,
  `nexus/model/providers/legacy_codex_cli.py`, and their tests
  (`tests/test_harness.py`, `tests/test_legacy_codex_provider.py`,
  `tests/test_native_cli.py`, `tests/test_provider.py`).
- **Modified:** `nexus/cli.py`, `nexus/__init__.py`, `nexus/runtime.py`,
  `nexus/session/manager.py`, `nexus/session/session.py`,
  `nexus/session/__init__.py`, `nexus/ext/manager.py`,
  `nexus/model/provider.py`, `nexus/model/providers/__init__.py`,
  `nexus/context/__init__.py`, `nexus/tools/manager.py`, `nexus/ui/__init__.py`,
  `pyproject.toml`, `README.md`, `SOUL.md`, `examples/python_api.py`, several
  `tests/`, and the baseline report fixtures.
- **New (untracked):** `nexus/host/` (`facade.py`, `protocol.py`,
  `supervisor.py`, `presence.py`, `daemon.py`,
  `transports/{uds.py,http_sse.py}`), `nexus/view/` (`model.py`, `reduce.py`,
  `fold.py`), `nexus/ui/cli/` (`app.py`, `approve.py`, `client.py`,
  `commands.py`, `keys.py`, `render.py`, `run.py`, `stream.py`, `theme.py`,
  `uds.py`), `nexus/ui/jsonl.py`, `nexus/session/export.py`, `ARCHITECTURE.md`,
  `EXTENDING.md`, `examples/{nexus.toml,mcp.json,hooks.toml,skills/,agents/,tools/}`,
  and the Phase 8 tests (`test_view_reduce.py`, `test_host_facade.py`,
  `test_host_supervisor.py`, `test_host_daemon.py`, `test_uds_transport.py`,
  `test_http_sse_transport.py`, `test_http_daemon_e2e.py`, `test_ext_trash.py`,
  `test_ui_cli.py`, `test_ui_daemon_e2e.py`, `test_ui_layering.py`,
  `test_session_operations.py`, `test_examples_valid.py`).

This ledger's own edit to `PLAN.md` is the only file it touches; every Phase 8
item above pre-existed it.

### 16.2 Phase checklist

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 Foundations | **Complete** (committed) | contracts, layered config, bus/registry/watch/cancel |
| 1 Own the loop | **Complete** (committed) | `core/loop.py`, sessions, Anthropic + scripted providers, router |
| 2 Tools and permissions | **Complete** (committed) | tool catalog, permission engine, adversarial suite |
| 3 Context and sessions | **Complete** (committed) | parts/budget/compact/cache, fork/replay, snapshots |
| 3.5 Session surface | **Complete** (committed) | `start_turn`/`subscribe`/`enqueue`, presence, session-scoped jobs |
| 4 Self-extension | **Complete** (committed) | manifest/quarantine/manager, hot tools, §6.5 money path test |
| 5 MCP | **Complete** (committed) | client/manager/bridge, failure isolation, injection wrapper |
| 5.5 Model registry | **Complete** (committed) | registry/tiers/ingest, degradation path |
| 6 Subagents and hooks | **Complete** (committed) | `Task` modes, seeded roles, bounding, hooks |
| 7 Provider breadth | **Complete** (committed) | Anthropic/OpenAI/Gemini/Ollama/opencode + conformance; legacy adapter deleted |
| 8 Surfaces and polish | **Implemented, uncommitted; line budgets unmet** | extension trash and opt-in daemon HTTP/SSE both landed (see §16.3); remaining gaps in §16.5 |

### 16.3 Phase 8 pieces as built (uncommitted)

- **Session (8a0):** `nexus/session/export.py` (json/markdown/jsonl, consistent
  read, no exclusive lock); `SessionManager` session trash
  (`delete`/`restore`/`list_trashed`/`purge_expired`, `TrashRecord`,
  retention); `Session.fail_turn` for a durable synthetic terminal on a
  supervisor start failure. The Phase 3.5 surface (`start_turn`/`subscribe`/
  `enqueue`/presence) was already committed.
- **View (8a1):** `nexus/view/{model,reduce,fold}.py` — pure, synchronous
  reducer importing only `nexus.events`; `nexus replay` renders from the log;
  golden tests in `tests/test_view_reduce.py` and `tests/fixtures/view/`.
- **Host (8a2):** `nexus/host/facade.py` (the verb list), `protocol.py`
  (msgspec Command/Result structs), `supervisor.py` (concurrent-turn
  scheduling/caps), `presence.py` (subscriber counts → derived attendance).
- **Extension trash (8c):** `ExtensionManager.trash` is scoped to the managed
  extension roots, refuses symlinks/traversal/non-candidates, moves the file
  atomically into a retention-recorded trash entry, and rebuilds; a failed
  rebuild rolls the move back unless `force=True`, and a pinned generation keeps
  the retired module alive until the last lease releases. `ExtensionTrashError` /
  `ExtensionTrashRecord` / `ExtensionTrashOutcome` and the `nexus ext trash`
  CLI action ride the facade. Tested in `tests/test_ext_trash.py`.
- **Daemon + UDS (8b):** `nexus/host/daemon.py` and
  `nexus/host/transports/uds.py`; auto-start, version handshake, stale-socket
  cleanup, idle shutdown; `nexus daemon status|stop|logs`. The prompt_toolkit
  CLI lives in `nexus/ui/cli/`; `nexus run`/`chat` are pure daemon clients with
  no in-process fallback; `ui/native.py` is retired.
- **CLI + JSONL (8b/8c):** `nexus/cli.py` rewritten as a client; `nexus/ui/cli/`
  (`app`, `approve`, `client`, `commands`, `keys`, `render`, `run`, `stream`,
  `theme`, `uds`); `nexus/ui/jsonl.py` `--json` passthrough. Slash commands as
  data in `commands.py`. `nexus doctor`, `nexus models`, `nexus sessions`,
  `nexus agents`, `nexus tools`, `nexus ext` implemented over the facade.
- **HTTP/SSE (8c):** `nexus/host/transports/http_sse.py` — a transport wrapping
  `HostFacade` (commands as POST, events as SSE with `Last-Event-ID` resume),
  loopback-only, constant-time bearer token, strict `Origin` allowlist,
  allocation bounds. The **daemon now starts and wires it** over the same facade
  the UDS socket serves when the surface is opted in (`NEXUS_HTTP=1`, the daemon
  entrypoint's `--http` flag, or `Daemon(http=True)`); it publishes a mode-`0600`
  `.http` discovery file (`default_http_path` / `read_http_endpoint`) and removes
  it on shutdown. Unit tests in `tests/test_http_sse_transport.py`; the live
  terminal-UDS-plus-HTTP pairing is a real-subprocess test in
  `tests/test_http_daemon_e2e.py`.
- **Docs:** `README.md` rewritten; `ARCHITECTURE.md` and `EXTENDING.md` added;
  `SOUL.md` rewritten to describe the as-built harness.
- **Legacy cutover:** `legacy_codex_cli.py`, the old `provider.py`, `agent.py`,
  `store.py`, and `ui/native.py` are deleted; the root package drops the legacy
  `Agent`/`CodexProvider` exports.
- **UI layering:** `tests/test_ui_layering.py` enforces that `nexus/ui/**`
  imports only `{nexus.host, nexus.view, nexus.events, stdlib}`.

### 16.4 Verification evidence

- **Full offline suite — re-reproduced after the Phase 8 delete/cancel
  hardening** (see §16.8) on 2026-09-23 at `fc26db7` + the uncommitted worktree:
  `3075 passed, 311 skipped, 2 deselected, 2 xfailed in 58.54s`, green
  (`rc=0`). The regenerated closeout report records
  `3075 passed, 311 skipped, 2 deselected, 2 xfailed in 57.69s` at head
  `fc26db7` (the pre-hardening figure was 3063 passed; the +12 are the new
  delete-refusal, parked-cancel, rollback, and trash-identity regressions). The
  2 deselected are the credential-gated `live` tests (`addopts = -m 'not live'`);
  the 311 skips are gated (absent credentials / model features), not failures.
- **The 2 xfailed are exactly the line-budget gates**, both `strict=False`:
  `tests/test_phase3_exit.py::test_line_budget_core_model_spec_within_plan_cap`
  and `::test_line_budget_host_view_ui_within_plan_cap`.
- **Line budgets (from the regenerated report, measured at `fc26db7` + tree):**
  - `core/ + model/ + tools/spec.py` = **12476 physical / 10326 code** vs the
    §11 cap of **2500** → over by 9976 (unchanged by the delete/cancel work).
  - `host/ + view/ + ui/` = **7899 physical / 6543 code** vs the §14.14 cap of
    **2000** → over by 5899 (surface baseline: host=4564, view=1585, ui=1750).
    This re-pins the baseline after the delete/cancel hardening; it is *not* a
    claim that any budget is met.
- **Security-review blockers:** `tests/test_security_regressions.py` pins the
  ten mandatory Phase 2 fixes (duplicate tool-call ids refused; Bash env overlay
  preserves the inherited environment; fs re-canonicalizes the permission key;
  atomic writes refuse a swapped/symlinked parent; `*_ALWAYS` grants cannot
  broaden; bounded killable Grep; symlink loop is a path-security failure; `~`
  in a rule is rejected; job registry eviction; only owned managers are closed).
  The approval-prompt tests were migrated to `nexus/ui/cli/approve.py` and pass.
- **Import cost:** `import nexus` ≈ 10 ms, no heavy modules; `import
  nexus.runtime` ≈ 99 ms.
- **HTTP + UDS on real subprocesses.** `tests/test_http_daemon_e2e.py` (6 tests)
  and `tests/test_ui_daemon_e2e.py` (15 tests) were run together: **21 passed in
  6.06s** (`rc=0`). They auto-start a *real* `python -m nexus.host.daemon`
  subprocess around the offline scripted provider and prove the §14.15/11
  end-to-end items: a terminal UDS view and an HTTP/SSE view subscribe to one
  live session (`test_terminal_uds_and_http_views_share_one_session`), the bearer
  token and `Origin` allowlist are enforced over real HTTP
  (`test_http_auth_and_origin_are_enforced`), a zero-view turn is replayed to a
  late SSE view (`test_zero_view_http_turn_is_replayed_to_a_late_sse_view`),
  `Last-Event-ID` resumes with no gap
  (`test_sse_last_event_id_resumes_without_a_gap`), and `SIGTERM` closes the
  surface and removes the mode-`0600` token file
  (`test_sigterm_closes_http_and_removes_the_token_file`,
  `test_restart_mints_a_fresh_token_and_keeps_the_session`).
- **Clean-workspace CLI smoke.** Against a throwaway workspace and `HOME`
  (`nexus init` → `doctor --json` → `ext list` → `daemon status --json` →
  `daemon stop`), the real `nexus` entrypoint exits cleanly; `doctor` reports an
  empty registry/extension state with no diagnostics and the daemon reports
  `running: true` with a live pid before `stop` returns `stopped`.
- **Daemon entrypoint `--http` smoke.** `python -m nexus.host.daemon --workspace
  ... --idle-timeout 3 --http --http-port 0` (no test harness) started, bound
  `127.0.0.1` on an ephemeral port, and published a mode-`0600` `.http` file
  carrying host/port/token/origins; a `SIGTERM` removed that file. This is the
  concrete basis for the corrected docs: the switch exists on the daemon
  entrypoint, not on the root `nexus` CLI.

### 16.5 True acceptance gaps (separate from the ledger)

These are the criteria not yet met; they are gaps, not waivers. Two earlier
entries — "HTTP/SSE is not daemon-wired" and "`nexus ext trash` is not
implemented" — are now closed and moved to §16.3; the §14.15 criterion-11
terminal-plus-HTTP pairing they blocked is now demonstrated by
`tests/test_http_daemon_e2e.py`.

1. **Line budgets unmet.** §12 criterion 10 (core < 2,500) and the §14.14
   surface cap (host/view/ui < 2,000) are both exceeded by a wide margin
   (12476 vs 2500; 7899 vs 2000). The gates remain non-strict `xfail` by design;
   the regenerated report records the exact overage.
2. **`/model` is cosmetic.** §14.11 lists `/model` as a session command, but the
   facade has no per-session model override; `nexus/ui/cli/app.py:_cmd_model`
   only lists selectable models and points the user at `[models] default` in
   `nexus.toml`. It deliberately never repaints a model the daemon is not using.
3. **`registry.mismatch` is not aggregated by `doctor`.** §15.5/§15.12 call for
   `nexus doctor` to surface accumulated catalogue defects. The loop emits
   `registry.mismatch` (with one capability retry), but `HostFacade.doctor`
   reports only registry *status* (source/models/stale) and does not collect or
   report the accumulated mismatches.
4. **Live provider end-to-end is offline-excluded.** §12 criterion 1
   (Anthropic + OpenAI-compatible + Gemini + Ollama, Codex absent) and the
   network-gated conformance runs are `-m live` and deselected in the offline
   suite; they were not exercised by the reproduced run. The offline conformance
   fixtures pass.

### 16.6 Additional material observations

- **Web frontend** is deferred exactly as §14.1/§14.16 state; its absence is not
  a gap.
- **The HTTP surface is a daemon-start decision, not a root-CLI switch.** The
  daemon entrypoint (`python -m nexus.host.daemon`) exposes `--http`,
  `--http-host`, `--http-port`, and repeatable `--http-origin`; the root `nexus`
  CLI is a pure client with no such flag. Earlier README/ARCHITECTURE/SOUL text
  said flatly "there is no CLI flag"; it has been corrected to distinguish the
  root CLI from the daemon entrypoint.
- **Extension trash is now landed** (see §16.3): `nexus/errors.py` and
  `nexus/ext/manager.py` carry the completed implementation and
  `tests/test_ext_trash.py` covers it. The in-flight note in an earlier revision
  of §16.5 is superseded.
- **Phase 8 was budgeted at ~13 days (§14.13) plus Phase 3.5's 3**, against the
  original §10 Phase 8's 5; §15.11 revised the total to ~59 working days.
- The two `xfail` line-budget tests are the **only** expected failures in the
  suite; everything else is green or gated.
- The legacy Codex config section still loads through a compatibility shim in
  `nexus/runtime.py` (`_is_legacy_codex_section`); this is config migration, not
  a retained Codex code path.

### 16.7 Phase 8 review hardening (Major findings)

The first independent Phase 8 review raised six Major findings; all are fixed
here with targeted regressions. The suite grew from 3043 to 3063 passed and the
baseline report is regenerated (§16.4). No requirement was relaxed.

- **`ext validate` no longer executes an arbitrary path.** `validate(target)`
  now routes through the same strict scoping as `trash`
  (`_scoped_candidate`): a target must be a discovered, in-root, non-symlink
  `*.py` candidate under `[ext].dirs`, so a config file, credential,
  `_`-prefixed file, traversal, or symlink is refused *before* the isolated
  importer is invoked. Covered at the manager, facade, and live HTTP/SSE layers
  (`test_ext_trash.py`, `test_http_sse_transport.py`). A latent
  `self._config` typo in the same method is fixed to `self._last_config`.
- **Trash metadata is no longer trusted.** Session and extension managers
  sanitize every on-disk field that later becomes a path (`trash_id`,
  `session_id`/source name, each artifact `files` entry), skip symlinked or
  staging entries, and revalidate an extension restore's destination against the
  managed roots. Malicious-metadata tests assert purge/restore/recovery never
  write or remove outside the trash/session/extension trees
  (`test_session_operations.py`, `test_ext_trash.py`).
- **`_publish_trash` hardening.** The staging directory's metadata is fsynced
  before the move and the moved file fsynced before publication/recovery;
  containment is re-derived after the swap (a parent replaced by a symlink fails
  closed); rollback only moves a file back when the original parent's identity
  is unchanged. Regressions cover the parent-swap refusal and the pre-publish
  fsync.
- **`EventSubscription.finish` cannot abort.** A full bounded queue drains and
  receives the terminal marker instead of raising `QueueFull`, so close, the
  reader loop, and unsubscribe never abort and a blocked consumer is always
  unblocked (`test_uds_transport.py`).
- **Supervisor retains no handles forever.** `Supervisor.forget(session_id)`
  drops a cached handle only when it has no active turn and no queued work; it
  is called by the facade on session delete, by a queue-dropping cancel, and
  when a watched turn finishes idle. A returning submission re-registers, so an
  idle session is never broken (`test_host_supervisor.py`).
- **Docs/policy.** The stale context-package comment was corrected; a missing
  `Origin` on the HTTP surface is documented as a deliberate strict refusal
  (already enforced and pinned by
  `test_origin_is_required_and_allowlisted_on_every_request`). The reload/report
  coalescing watermark now rolls back when a rebuild raises, so a later caller
  is never handed a stale report for a rebuild that never ran.

### 16.8 Phase 8 delete/cancel hardening (second review round)

A second independent review found that a deleted session could resurrect through
a durable queued submission or parked supervisor work, plus some trash-metadata
and extension-trash rollback gaps. All are fixed with targeted regressions; the
suite grew from 3063 to **3075 passed** and the baseline report is regenerated
(§16.4). No requirement was relaxed.

- **`HostFacade.delete` refuses scheduled/durable work.** It rejects a session
  the supervisor still holds work for — a running turn or a parked submission —
  and a live session with persisted queued input (`SessionManager.queued_depth`),
  raising `SessionBusy` before delegating. Deleting under that work would let a
  durable submission start against a trashed session. The refusal applies even
  with `force=True`; the caller must cancel explicitly (dropping the queue)
  first. Covered in `test_host_facade.py` with `max_concurrent_turns=1`: A
  active, B parked, both deletes refused, then B cancelled while idle parked and
  deleted and A allowed to finish with no second provider script ever consumed
  (`provider.calls == 1`), plus a session-only-queued refusal.
- **`SessionManager.delete` refuses queued input independently, including
  `force`.** Before and under the exclusive lock it rejects a live handle with
  `queue_depth > 0`, so a delete can never strand an `input.queued` that a later
  open would rehydrate and run. The refusal leaves the log and the in-memory
  FIFO intact and is repeatable after a reload; an explicit
  `cancel(drop_queue=True)` is the only way through. Covered by
  `test_session_operations.py` (force, reload/rehydrate, and the emitted
  `input.dropped`).
- **`Supervisor.cancel` on an idle parked session drops its durable queue.** When
  no turn is in flight but the session's own FIFO holds pending input, the cancel
  now routes through `session.cancel(drop_queue=True)` so it emits
  `input.dropped`; previously only the supervisor's queue was dropped and a
  reload could execute a submission a cancel explicitly dropped. Covered by
  `test_host_supervisor.py` (a real session parked behind a gated holder;
  rehydrate is empty and the pre-assigned turn id never started).
- **Trash entry identity.** Session and extension trash listings now additionally
  require `entry.name == record.trash_id`, so a crafted record whose `trash_id`
  names a different entry is ignored by list/purge/restore rather than trusted.
  Covered in `test_session_operations.py` and `test_ext_trash.py` with positive
  (published name equals id) and mismatched (entry ignored, file untouched)
  shapes.
- **Extension-trash rollback on a raising/cancelled reload.** A `reload` that
  raises or is cancelled now rolls the moved file back before the exception
  propagates, and the rollback is best-effort so it never masks the original
  error — a cancellation still propagates. Covered by `test_ext_trash.py`.
- **`ext.enabled = false` guard.** `ExtensionManager.validate` returns a truthful
  empty pass for a whole-tree check and refuses a named target;
  `ExtensionManager.trash` refuses with `ExtensionTrashError`. Covered by
  `test_ext_trash.py`.

### 16.9 Phase 8 retirement/cancel hardening (third review round)

A third independent review found two blockers and two minor items; all are fixed
with targeted regressions. The full offline suite is green at **3083 passed, 311
skipped, 2 deselected, 2 xfailed in 61.30s** (`rc=0`) and the closeout report
`tests/fixtures/reports/phase3_exit_baseline.{json,txt}` is regenerated (its
embedded full-suite run agrees: 3083 passed; surface baseline host=4640). No
requirement was relaxed.

- **A force-deleted session could resurrect through a late viewer cleanup.**
  `SessionManager.delete` now **retires** the live handle before any artifact
  moves, and a retired `Session` refuses every write path:
  `append_event`/`append_message`/`append_summary`, `enqueue`,
  `start_turn`/`send`, `recover_dangling_tool_uses`, and the unattended
  fallback. `Session._emit` becomes a no-op once retired, so a disconnecting
  viewer's `presence.left`/`presence.changed` cleanup — the exact path that used
  to recreate the moved log as a presence-only file — cannot write. `begin_turn`
  re-checks retirement under the exclusive lock, closing the window where an
  in-flight start could append after the move. A failed delete rolls the
  retirement back (`unretire`), so a refused/failed delete never strands a
  usable handle. Regressions: `tests/test_session_operations.py` (force-delete
  then view disconnect does not recreate the log; a failed move rolls back
  retirement and the handle still writes; a retired handle refuses a late
  `start_turn`) and `tests/test_host_facade.py` (the facade
  delete→view-disconnect path).
- **`Supervisor.cancel` racing `_start` before `session.active` existed lost the
  cancellation.** The window between the supervisor marking a session active and
  `session.start_turn` installing a lease (it yields in `ensure_ready`/hook
  gates) made `cancel` observe `active is False` and drop the request, so the
  turn ran to completion. The supervisor now records a cancel that lands while a
  start is in flight (`_starting`/`_cancel_requested`) and `_start` honours it
  the moment the lease exists, cancelling the just-started turn; the durable
  queue is dropped then, so a queue-consuming start is consumed into the
  cancelled turn rather than stranded or silently run. `forget` clears the
  in-flight bookkeeping. `cancel`'s returned `dropped` count now also counts a
  session-only durable queue that has no supervisor pending, without
  double-counting mirrored entries. Regressions: `tests/test_host_supervisor.py`
  — a deterministic `ensure_ready` barrier proves cancel-before-lease cancels
  the turn; a queued consume stays consistent (`input.queued` + `input.consumed`,
  no `input.dropped`, no rehydrate); and a session-only durable queue reports
  `dropped == 2`.
- **Minor: empty session-trash records are refused.** `_trustworthy_trash_record`
  now rejects a record naming no artifacts, which this manager could not have
  written and whose restore would silently move nothing
  (`tests/test_session_operations.py`).

### 16.10 Phase 8 consistency follow-up (fourth review round)

A fourth, narrow follow-up review found three correctness bugs and one latent
crash-recovery hazard; all are fixed with focused deterministic regressions. The
full offline suite is green at **3092 passed, 311 skipped, 2 deselected, 2
xfailed in 58.90s** (`rc=0`) and the closeout report
`tests/fixtures/reports/phase3_exit_baseline.{json,txt}` is regenerated
(`host=4694 view=1585 ui=1750`). No requirement was relaxed.

- **First-responder lease key mismatch on detach.** `Presence.detach` released a
  departing view's leases by the attachment **token**, but `claim` keys a lease
  by the `client_id` the claimant passed. A view that claimed an approval and
  then disconnected left the lease held by a ghost, so a live view still lost the
  race against a dead one — the exact case PLAN §14.6 says must be robust. Detach
  now releases `self._held[client_id]`, and only when the last attachment for
  that client leaves (a shared `client_id` is not identity, so a still-attached
  view keeps the lease). Regressions: `tests/test_host_facade.py`
  (`test_presence_detach_releases_the_lease_the_view_held` and the
  shared-client-id case).
- **In-flight cancel dropped count was not authoritative.** A `Supervisor.cancel`
  racing a start recorded a pre-honor estimate of `dropped`; if the in-flight
  start then failed, its queued head was dropped rather than consumed and the
  synchronous return was one short. `_start` now reconciles the count when it
  honours the recorded cancel — reading the durable FIFO before and after
  `session.cancel` and adding the exact delta to the supervisor-only submissions
  already removed — and reports it on `daemon.session_cancelled`. The
  synchronous return stays the documented pre-honor estimate. Regressions:
  `tests/test_host_supervisor.py` (a gated start that fails reports `dropped == 1`
  matching `input.dropped`; a start that succeeds reports `0` with
  `input.consumed`).
- **`restore` no longer claims success while moving nothing.** A trash record
  listing an artifact that is missing or a symlink used to be *skipped*; if every
  listed artifact was skipped the entry was removed and the session id returned —
  reporting success while destroying the authoritative trash entry. `restore` now
  refuses the whole operation when a listed artifact is absent or not a regular
  file (re-checked under the lock), leaving the entry intact. Regressions:
  `tests/test_session_operations.py` (missing and symlinked listed artifacts
  refuse; the entry survives).
- **Crash-mid-delete staging is recovered before `open`/`list`.** `delete` moves
  artifacts into a `.staging-*` directory and publishes it atomically, but
  `_recover_trash` only ran on trash operations. A crash before publication left
  the authoritative log staged out of the sessions directory; the next
  `open(..., create=True)` published a *fresh empty* `.jsonl` first and shadowed
  it (title/records silently lost), and `list` simply skipped it. `SessionManager.open`
  and `list` now call `_recover_trash` before create/migrate/enumerate.
  `_recover_trash` only scans the trash directory and takes no session/handle
  lock, so it cannot recurse or deadlock the `_handles_lock`. Regressions:
  `tests/test_session_operations.py` (`open` recovers the authoritative log
  instead of shadowing it; `list` surfaces it).
- **Artifact enumeration is now consistent.** `_artifacts_exist` omitted the
  `.v1.bak` backup that `_artifact_paths` moves and `_session_artifact_names`
  trusts, so a session whose only surviving artifact was its migration backup was
  refused by `summary`/`export`/`delete`/`restore` even though delete could move
  it. `_artifacts_exist` now includes it. Regression:
  `tests/test_session_operations.py` (`test_a_v1_backup_alone_is_a_deletable_restorable_session`).
- **Rollback never writes staging metadata into the sessions directory.**
  `_recover_trash`'s untrusted-metadata rollback moved every non-symlink staging
  child — including `meta.json` — into the sessions directory. It now removes the
  metadata document instead (pinned by the extended
  `test_trash_staging_without_meta_is_rolled_back`).

**Deliberately left for follow-up (not edited):** `_recover_trash` is not
serialized by any cross-process lock. Two daemons recovering the same staging
directory could each pass the `final.exists()`/`destination.exists()` checks and
then race `os.replace`; since each artifact appears in exactly one staging
directory the worst case is a redundant move of an already-existing destination,
but a concurrent recoverer cannot restore cross-artifact atomicity. A safe fix
(an exclusive recovery lock, or an owner-only recovery marker) would add locking
to a function currently called lock-free from `open`/`list` and risks the very
recursive-lock/deadlock class this round avoided, so it is described here rather
than changed. Likewise, `supervisor.cancel`'s synchronous return remains a
pre-honor *estimate* for the in-flight case (the authoritative count is on
`daemon.session_cancelled`); refactoring `cancel` into a deferred async
completion was out of scope for a narrow follow-up.

---

## 17. Amendment: genuine per-session model switching (Phase 8 gap closure)

Fourth append-only amendment, same rules as §14-§16: sections 1-16 are **not**
rewritten, only amended by reference. This section records one packet's work:
closing §16.5 item 2 ("`/model` is cosmetic"). `nexus doctor`'s aggregation of
`registry.mismatch` (§16.5 item 3) is deliberately out of scope here and left to
a subsequent packet.

### 17.1 What was wrong

§16.5 recorded that `/model` only listed selectable models and told the user to
edit `[models] default` in `nexus.toml`: the facade had no per-session override,
so the CLI never repainted a model the daemon was not using. The gap was a
missing primitive, not a UI bug.

### 17.2 What was built

A durable, validated **per-session model selection** threaded through every
layer the contract requires, with no new privileges:

| Layer | Change |
| --- | --- |
| Event catalogue | `model.selected` added to `MODEL_EVENTS` (`nexus/events.py`). Session-scoped and turn-less, like `input.*`/`presence.*`. |
| Model (L1) | `nexus/model/selection.py` — frozen `ModelSelection` `{reference, provider, model, tier, tier_source, requested_tier, clamped}` with `to_dict`/`from_dict`. Descriptive only; no credential/endpoint/sampling field can be present. |
| Session (L3) | `Session.select_model()` persists the event; `Session.model_selection` is the rehydrated override. Rehydration is a pure log read on open; `_observe_event` keeps the in-memory mirror current. |
| Runtime (L4) | `Runtime.select_session_model()` validates via the **same router** a configured default uses (provider/alias/tier rules), refusing an unknown provider, a malformed reference, or a tier with no runnable model; a tier name without a `[models]` registry is refused rather than silently reinterpreted as a model id. |
| Host protocol | `ModelSelect` command + `ModelSelectResult` (redacted; carries the configured fallback chain and `apply_next_turn`). `HostFacade.select_model()` and `_dispatch` route it. |
| Client / UI | `Client.select_model()`; `/model` lists with `list` and chooses with `/model <tier|provider/model|id>`, repainting the status line; `nexus models select <ref> --session <id>` is the root CLI entry. |
| View | `_on_model_selected` records the choice so a replay shows it before any turn. |

### 17.3 Guarantees held

- **One durable event.** Each selection appends exactly one `model.selected`; a
  reopen rehydrates the newest and writes nothing. Replay is exact.
- **Frozen per turn.** The override is read once at turn start and handed to both
  the static and the manifest per-iteration environment paths. A selection made
  while a turn is in flight is invisible to that turn and takes effect on the
  next.
- **Tier ceiling.** The session's selected tier becomes the parent tier for its
  subagents (in addition to `agents.max_tier`), so a child can never exceed the
  model the session actually runs. Permission rules are untouched and remain
  absolute.
- **Fallback preserved.** The effective model still flows through the ordinary
  `provider_for`/`model.fallback` path; the fallback chain is reported with the
  selection.
- **No secret exposure.** The selection crosses the facade as descriptors only;
  resolution failures surface as redacted `ErrorResult`s.
- **Layering preserved.** Still no in-process client fallback; `nexus/ui/**`
  imports only `nexus.host`/`nexus.view`/`nexus.events` (the layering test is
  unchanged and green).

### 17.4 Verification

New offline coverage: `tests/test_session_model.py` (14 tests: validation,
one-event persistence, reopen rehydration, effective model, in-flight isolation,
two-session isolation, tier resolution and the subagent tier ceiling, replay
state, fallback reporting, no-secret fields); three real-daemon UDS tests in
`tests/test_ui_daemon_e2e.py` (select over the wire with reconnect rehydration,
per-session isolation, unknown-reference refusal); two interactive-CLI tests and
one root-CLI test (`tests/test_ui_cli.py`, `tests/test_cli.py`); the protocol
round-trip and every-verb facade tests were extended.

Full offline suite: **3114 passed, 311 skipped, 2 deselected, 2 xfailed in
63.92s** (`rc=0`). The two `xfail`s remain exactly the (non-strict) line-budget
gates. The closeout report
`tests/fixtures/reports/phase3_exit_baseline.{json,txt}` was regenerated after
green: `core+model+spec = 12554` physical / `host+view+ui = 8190`
(`host=4768 view=1604 ui=1818`). Both are still over the plan caps; the packet
neither met nor waived a budget, only moved the recorded baseline.

This closes §16.5 item 2. §16.5 item 3 (`doctor` and `registry.mismatch`) is
untouched by this packet.

### 17.5 `doctor` aggregation of `registry.mismatch` (§16.5 item 3 closure)

A later packet in the same §17 amendment closes §16.5 item 3. `HostFacade.doctor`
now reports accumulated catalogue defects in addition to registry *status*.

- **New module.** `nexus/host/doctor.py` owns a `mismatch_summary(sessions_dir)`
  read. It never opens a session handle (so a health check never migrates,
  recovers, or writes); it only reads bounded bytes.
- **Bounded.** `ScanLimits` caps discovered logs (`max_sessions`), each tail read
  (`max_bytes_per_log`), parsed records (`max_lines_per_log`), tally keys
  (`max_groups`), and retained samples (`max_samples`); any cap hit sets
  `truncated`, so an incomplete scan is stated rather than hidden.
- **Resilient.** A missing/inaccessible directory, a symlinked log, an
  interior-corrupt log, or a crash tail is skipped; a malformed individual line
  is skipped in place. Nothing raises.
- **Redacted.** Only a count, grouped provider/model/reason/session tallies, and a
  bounded sample of `session`/`seq`/`ts` plus those descriptive fields cross the
  boundary. The event's raw `detail` (which may echo a provider error) is
  dropped, and every surfaced string is sanitized and secret-redacted. Duplicate
  events (by `id`) are folded once.
- **Surfaces.** `DoctorResult.report["registry_mismatches"]` carries the summary;
  the human `nexus doctor` output prints counts, per-field tallies, and samples,
  and states `none` when clean; `nexus doctor --json` emits the same summary.

Verification: `tests/test_doctor_mismatches.py` (21 tests: counts/groupings,
empty/opaque/absent directory, non-mismatch records, duplicate events, an open
session read without a handle, corrupt tails and bad lines, interior corruption,
symlinked logs, secret-shaped hostile fields, and every bound) plus a real-runtime
facade test in `tests/test_host_facade.py` and an end-to-end daemon+CLI test in
`tests/test_cli.py` (human and `--json`, with a secret-bearing log asserted not
to leak). Full offline suite: **3137 passed, 311 skipped, 2 deselected, 2
xfailed**. The baseline report was regenerated after green (§16.4); the new
`nexus/host/doctor.py` moved the recorded surface baseline to `host+view+ui =
8526` physical (`host=5104 view=1604 ui=1818`), still over the plan cap.

This closes §16.5 item 3.

### 17.6 Review hardening of the §17.4/§17.5 packets

A review of the two packets above found a small set of defects; this packet fixes
them without changing either feature's contract. No commit was made.

**`doctor` reads.** `_read_tail` now opens the log by descriptor with
`O_NONBLOCK | O_NOFOLLOW` (each `getattr`-optional so a platform without it still
runs), `fstat`s the descriptor, and refuses anything that is not `S_ISREG` before
reading. A symlink can no longer be followed (closing the `stat`-then-read race),
a named FIFO or device can no longer block the open, and the read is capped at
`max_bytes` from a size taken on the same descriptor, so a log that grows during
the scan cannot be read past the cap. The descriptor is always closed and every
`OSError` becomes `None` (skip) rather than a raise.

**`doctor` bounds and enumeration.** `ScanLimits` now rejects a zero, negative,
non-integer, or boolean cap in `__post_init__` (`ValueError`), so a zero cap can
never silently mean "unbounded". `_candidate_logs` enumerates directory entry
*names* only, sorts the valid ids, and then slices, so exactly which logs are
scanned no longer depends on filesystem iteration order; `truncated` is true iff
more valid logs existed than the cap.

**`doctor` offload.** `HostFacade.doctor` stays a synchronous method (its direct
callers are unchanged), but the wire path in `_dispatch` now runs it through
`asyncio.to_thread`, so reading up to 64 x 512 KiB and parsing their JSON cannot
stall the daemon event loop. This was feasible without altering the sync facade
contract, so it was implemented rather than waived.

**Model selection minors.** The `ModelSelection.clamped` comment no longer claims
a ceiling is applied (a session selection is never clamped today; the field is
kept, documented as reserved, so the persisted/wire shape needs no migration).
`from_dict` deserializes `clamped` strictly -- only a JSON boolean counts, so a
hostile `"false"`/`1` reads as `False`. The reported fallback chain is now
sanitized, secret-redacted, deduplicated, and has the selected reference dropped
(a fallback to the model already in use is not a fallback). The unused
`Runtime.session_model` helper (which reached into `SessionManager._handles`) was
removed.

**Verification.** New coverage: `tests/test_doctor_mismatches.py` adds a named
FIFO that must not block, a concurrently growing log that must stay bounded,
deterministic enumeration, and zero/negative/non-integer cap rejection;
`tests/test_session_model.py` adds strict-bool parsing and fallback
redaction/dedupe/selection-drop. Full offline suite: **3148 passed, 311 skipped,
2 deselected, 2 xfailed** (`rc=0`). The baseline report was regenerated after
green: `core+model+spec = 12560` physical / `10385` code, `host+view+ui = 8620`
physical / `7126` code (`host=5198 view=1604 ui=1818`); both remain over the plan
caps and no budget was met or waived.
