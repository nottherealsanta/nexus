# The loop and turns

`nexus/core/` is transport-independent plumbing. The loop (`core/loop.py`,
`run_turn`) drives one turn and imports **no concrete manager**: it is written
against the protocols `SessionView`, `ContextAssembler`, `ProviderResolver`,
`EventSink`, `ToolDispatcher`, `PermissionGate`, `HookRunner`, `ManifestRefView`
and `EnvironmentFactory`. `runtime.py` supplies the implementations.

## Files

| File | Owns |
| --- | --- |
| `core/loop.py` | `run_turn`, the protocols, stream collection, capability degradation, tool batch handling, terminal events |
| `core/turn.py` | `TurnState` (immutable, serializable), `TurnLimits`, `TurnUsage`, `TurnOutcome`, legal phase transitions |
| `core/bus.py` | `Bus`/`Subscription`: bounded async fan-out (`DROP_OLDEST` / `DROP_NEWEST`) |
| `core/cancel.py` | `CancelToken`: cooperative cancellation |
| `core/registry.py` | `Registry` (immutable, generation-stamped) and `RegistryRef` (atomic reference) |
| `core/watch.py` | `DirectoryWatcher`: dependency-free mtime+size polling |

## One iteration

```
emit turn.started; persist the user message (unless the caller already did)
while not terminal:
    if limits exceeded → complete("budget" | "max_iterations")
    cancel.raise_if_cancelled(); consume steering messages
    pin ONE manifest generation; build the iteration environment from it
    request = await assemble(session)             # PreCompact gate runs inside
    emit context.assembled (+ context.compacted, ContextAssembled hook)
    for candidate in [primary, *fallbacks]:       # fallback only on a provider error with no output yet
        emit model.started; stream with capability degradation
    persist assistant Message           ← durable BEFORE any tool handling
    emit text / thinking / model.usage / model.stopped
    no tool uses → steering? continue : complete
    emit tool.requested + tool.input per call
    PreToolUse hooks over the whole batch → prepare → permission plan for the WHOLE batch
    resolve every ASK durably (permission.requested / .resolved)
    dispatch approved calls (concurrency planned) → PostToolUse hooks
    persist ONE user message of ToolResult blocks, in call order; emit tool.result
    release the manifest pin; iteration += 1
finally: cancel pending approvals, TurnEnd hook, emit exactly one terminal event, release the lease
```

### Why these details

- **One pinned manifest generation per iteration.** A reload that commits
  mid-iteration is invisible until the next one; a tool written in iteration N is
  callable in N+1 of the same turn, no restart ([extensions.md](extensions.md)).
- **Assistant message first.** A crash leaves a dangling `ToolUse`; resume
  appends an error `ToolResult` for each unresolved call without executing it.
- **Batch-wide permission check** so one approval covers several calls and a
  denial never leaves half a batch executed.
- **Concurrency planning** runs parallel-safe tools together and `exclusive`
  (mutating) tools alone ([tools.md](tools.md)).
- **Lease discipline.** The loop owns exactly one turn lease (`session.begin_turn`
  if none is supplied) and releases it on every exit path.

## Turn state and limits

Phases: `new → awaiting_model ⇄ awaiting_tools → completed | failed | cancelled`
(`_ALLOWED_TRANSITIONS` in `core/turn.py`; terminal phases cannot be resurrected).
Stop reasons: `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`, `refusal`,
`error`, `budget`, `max_iterations`, `cancelled`.

`TurnLimits` (defaults from `[agent]`): `max_iterations` 0 (unlimited; a positive value caps model iterations), `max_seconds` 1800,
optional `max_input_tokens`, `max_output_tokens`, `max_total_tokens`. `exceeded()`
returns the first tripped limit; the loop records `max_iterations` or `budget`.
`TurnUsage` is additive across iterations (input, output, cache read/write,
reasoning). The completed-turn footer's `turn ↑… ↓…` is this cumulative usage;
the composer context meter instead shows the latest request's prompt size and
context-window position, so the two numbers are not expected to match.

## Failure handling

| Failure | Response |
| --- | --- |
| Transport, 408/409/425/429/5xx | Bounded jittered retry inside `model/http.py` (`RetryPolicy`: 4 attempts, 0.5s base, 30s cap). Never mid-stream. |
| Explicit OpenAI-compatible stream overload (`service_unavailable` / `overloaded`) | Retry the same request after 2, 4 and 8 seconds (three retries). Waiting is cancellable; `model.retrying` records attempt and delay. Partial text stays visible as an interrupted attempt; unfinished tool calls are discarded before retry and never executed. Persistent overload fails after the bound. |
| Provider error before any output | Try the next fallback (`model.retrying`, `context.degraded` with `feature: provider_failure`). A refusal or partial stream never falls back. |
| Provider rejects a capability the registry claimed | `CapabilityRejected`: one retry without the feature, `registry.mismatch`, the turn completes. Adapters declare a degradation policy `drop` / `to_text` / `error` per block type. |
| Provider switched mid-session | Allowed, lossy, visible: `context.degraded` with `feature: provider_switch`. |
| Model has `tools = false` | Schemas are omitted; any call it still makes gets an error result (`tools_unsupported`), nothing runs. |
| Malformed tool JSON | Durable error `ToolResult`; counts against `malformed_budget` (`DEFAULT_MALFORMED_BUDGET`); over budget fails the turn. |
| Duplicate tool-call ids | Re-identified before persistence; duplicates become error results, not executables. |
| Unknown tool, tool raises or times out, permission denied | Error result the model sees (the denial names the rule to grant); the turn continues. |
| `PreCompact` hook blocks | Turn fails with an actionable message before the model is called. |
| Context overflow | Compaction; if priority-0 parts alone exceed the budget, an actionable `ContextOverflow` failure ([context.md](context.md)). |
| `stop_reason: error` | Turn fails. |
| Cancellation | Cooperative, raced against every stream `__anext__`; `turn.cancelled`. Tool subprocesses get SIGTERM then SIGKILL by process group. |
| Any other exception | Harness failure: `turn.failed` with a URL-userinfo-redacted message. The loop never crashes the daemon. |

Principle: **tool-level failures are model-visible results; harness-level
failures end the turn.**

## Steering, queue, interrupt

Input submitted during a turn is durable (`input.queued`). Three modes
(`HostFacade.enqueue`, default `steer`): `queue` (runs as a new turn at the boundary), `steer`
(`consume_steering` injects it at the next model step, after the current
operation), `interrupt` (cancels the active turn without dropping other queued
input, then runs first). `input.started` / `input.consumed` / `input.dropped`
record the lifecycle. See [surfaces.md](surfaces.md#messages-during-a-turn).

## Context accounting

Two sources reach the UI meter. `context.assembled` carries the assembler's
estimate (`used_tokens`, a character heuristic) with `input_budget` and
`context_window`. `model.usage` carries `prompt`, the provider's own count of the
whole request; an adapter whose `input` excludes cache tokens sets
`usage_input_excludes_cache = True` (Anthropic) and the loop adds cache reads and
writes back. The reducer stores that as `measured_tokens` and carries it into the
next assembly, so `ui_support/context.py:context_measure` shows a provider number
whenever one exists.

## Thinking

Responses-API requests for thinking-capable models ask for
`reasoning.summary = "auto"`; Gemini requests `includeThoughts` (an explicit zero
budget disables it). Provider summary deltas become durable thinking blocks. The
loop emits `thinking.end` at a provider boundary or when an unsigned thought
stream turns into text or tools, so replay reproduces the boundaries without
duplicating the final aggregate. Summaries are provider-supplied and may be
absent; no duration is invented. The Claude Agent SDK bridge buffers text and
exposes no live thoughts. Mapping rules: [provider-onboarding.md](provider-onboarding.md).

## Long-running `bash`

A foreground `bash` blocks until exit, the yield window (`tools.bash_yield_s`,
default 120s) or cancel. At the window a running command is **yielded**: it
becomes a background job and the result says `status: running` with a `job_id`.
See [tools.md](tools.md#shell-jobs).

## Post-tool continuation reliability

Deterministic core tests verify that successful and error tool results, including
a simulated subagent exhausting its iteration limit, reach the parent's next
model request exactly once. A normal answer completes; a provider exception or
`MessageStop(error)` fails. Bare EOF and an empty `MessageStop(end_turn)` complete
without an answer. These cases release the lease and emit one terminal event;
none retries the provider or redispatches the tool.

Provider inspection confirms OpenAI Chat/Responses synthesize a final
`MessageStop` even for an empty stream, defaulting to `end_turn`; collected tools
promote an absent/`end_turn` stop to `tool_use`. Anthropic also defaults its final
stop to `end_turn`. The reproduced core bug was an explicit error stop with
collected tool calls: it previously dispatched them and continued. It now fails
before dispatch, with a regression test. The original reported post-tool stall
was not reproduced by these tests and remains unverified; no speculative retry
was added. Provider tests here use fixtures, not live service requests.

## Changing the loop

- New loop behavior needs a protocol method, not an import of a manager.
- Every new observable state is an event ([events-and-view.md](events-and-view.md)).
- Tests: `tests/test_core_*.py`, `test_capability_degradation.py`, `test_hooks_*`;
  drive with `ScriptedProvider` ([testing.md](testing.md)).
