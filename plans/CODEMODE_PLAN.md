# Code mode plan (Pydantic Monty)

## Goal and scope

Add **code mode**: the model can write a short Python program that calls
Nexus tools as async functions, instead of calling tools one at a time. The
program runs in [Pydantic Monty](https://github.com/pydantic/monty), a
sandboxed Python interpreter written in Rust. Only the program's result and
printed output return to the model's context. Intermediate tool results stay
out of that context.

Why: loops, fan-out (`asyncio.gather`), filtering, and joining tool output
take one model round trip instead of many. This saves tokens and latency on
work like "grep these 40 files, read the ones that match, return a table".

**Hard requirement (from the user):** a tool called from code follows the
**same permissions** and is **run by Nexus the same way** as a tool the model
calls directly. That means the same `PreToolUse` hooks, `ToolManager.prepare`
validation, permission gate plan, durable `permission.requested` /
`permission.resolved` approvals, execution-time path recheck, dispatch,
timeouts, result caps, cancellation, `PostToolUse` hooks, and durable events.
The sandbox never gets a second way to reach the filesystem, network, or
processes.

Out of scope for v1:

- Session state that persists across code calls (REPL variables). This is
  in-memory only and would break "replay reproduces live state"; see
  [Later](#later).
- Mounting workspace directories into Monty. File access goes through
  `read`/`write`/`edit`, so `tools/permissions.py` stays the only path check.
- Third-party packages inside the sandbox (`install_dependencies`).

## What Monty 1.0 gives us (verified locally)

`pydantic-monty==1.0.0` (released; 0.0.x was the preview line) was installed
in a scratch venv on macOS arm64 / Python 3.13. The following was checked:

| Checked | Result |
| --- | --- |
| `AsyncMonty()` pool → `pool.checkout(limits=…)` → `await session.feed_run(code, external_lookup={...})` | Works. Pool start ≈ 10 ms. |
| Async host functions, called from sandbox code with kwargs | Awaited on the host event loop. Return values (dict/list/str/int) cross cleanly. |
| `await asyncio.gather(read(...), read(...))` inside the sandbox | Host coroutines run **concurrently** (2 × 200 ms sleeps finished in 0.21 s). |
| Host function raises `PermissionError` | The sandbox can catch it as `PermissionError`. |
| `print_callback` | Receives `(stream, text)` per print. |
| Trailing expression | Returned as the `feed_run` value. |
| `open('/etc/passwd')`, `os.listdir('/')` with no mount/`os` handler | `PermissionError` inside the sandbox. |
| `import subprocess` | `ModuleNotFoundError`. |
| `while True: pass` with `max_feed_duration_secs=5` | `TimeoutError` in the sandbox → `MontyRuntimeError`. |

Relevant API facts from the 1.0 stubs:

- Execution always happens in **worker subprocesses** (`monty` binary from
  the `pydantic-monty-runtime` wheel). A crash raises `MontyCrashedError`,
  and the daemon process is never at risk.
- `ResourceLimits`: `max_feed_duration_secs`, `max_turn_duration_secs`,
  `max_memory`, `max_recursion_depth`, `max_suspensions` (external calls per
  checkout, default 1000), `max_total_sleep_secs`. The duration clock runs
  **only while sandbox code executes**, not while it is suspended waiting on
  the host. So a nested tool call that is waiting for user approval does not
  use up the CPU budget.
- `checkout(type_check=True, type_check_stubs=…)` runs `ty` over the snippet
  against stub declarations before execution. This gives the model a real
  type error for a wrong tool argument before any tool runs.
- `OSPolicy` controls clock, `sleep` (`'zero'` / capped `'system'`), and
  random seeding.
- Error types: `MontySyntaxError`, `MontyTypingError`, `MontyRuntimeError`
  (with `.display('traceback')`), `MontyCrashedError(timed_out=…)`.

Still to verify in Phase 0 (not yet tested):

1. `request_timeout` is a "per-turn parent-side deadline". Confirm it does not
   kill a worker that is parked on a long host await (for example, a
   5-minute approval wait). If it does, leave it unset and rely on the
   sandbox limits plus Nexus's own wall clock.
2. What happens when the awaiting asyncio task is **cancelled** in the middle
   of `feed_run`. Is the worker killed or returned dirty? Always discard the
   session after a cancel.
3. The `monty` binary resolves under `uv tool install nexus[codemode]` and
   `pipx`, not only in a dev venv.
4. Wheels exist for every platform in the release matrix (see
   `docs/release.md`).

## Design

### 1. One tool pipeline, shared by the loop and by code

Today `run_turn` (`core/loop.py`, around lines 2210–2440) runs this pipeline
inline for a model batch:

```
PreToolUse hooks → prepare → gate.plan → (fail_turn?) → unattended audit
→ apply_plan → open asks + permission.requested → await_decision
→ permission.resolved → with_decisions → gate.authorize → dispatch
→ PostToolUse hooks → merge in original order
```

**Step 1 (pure refactor):** move this block into one function in
`core/loop.py`:

```python
async def _execute_tool_batch(
    uses: Sequence[ToolUse], *, tools: ToolDispatcher, gate: PermissionGate,
    hooks: HookRunner | None, emitter: _Emitter, token: CancelToken,
    parallel_allowed: bool, session_id: str, turn_id: str,
    tool_emit: Callable[..., Awaitable[None]],
    invoke_tool: ToolInvoker | None = None,
) -> BatchOutcome   # results in original order + malformed count + fail_turn reason
```

`run_turn` calls it and keeps doing the parts that belong to a model batch:
appending the `role="user"` result message, `_emit_tool_results`, the
malformed budget, and turning a `fail_turn` into `state.fail(...)`. No
behavior change. The existing loop, permission, and hook tests must pass
unchanged.

**Step 2:** code-mode calls use **the same function** with a one-call batch.
There is no second permission or dispatch path, so the requirement is met
by construction.

### 2. The `invoke_tool` seam (already declared, never wired)

`ToolContext.invoke_tool` already exists in `tools/spec.py` ("Supplied by the
tool manager packet; `None` until then") and is unused. Wire it:

- In `core/loop.py`, per iteration, build a `ToolInvoker` closure bound to
  that iteration's `tools`, `gate`, `hooks`, `emitter`, `token`, and a
  **parent call id**. It builds `ToolUse(id=f"{parent_id}.{n}", name, input)`
  and runs `_execute_tool_batch([use], …)`. It returns the single
  `ToolResult` plus the prepared spec (for `mutates`/`concurrency`).
- Hand it down through `ToolDispatcher.dispatch(…, invoke_tool=factory)`.
  This is a new optional keyword, duck-typed the same way `preview` is.
  `_ToolDispatcherAdapter._ctx_factory` (`runtime.py`) sets
  `ToolContext.invoke_tool = factory(call.id)` for the codemode tool only.
  Other tools keep `None`.
- Layering stays one-way. `core/loop.py` still imports no concrete manager,
  and the tools layer sees only a structural `ToolInvokerView` protocol added
  to `tools/spec.py`:

```python
class ToolInvokerView(Protocol):
    def specs(self) -> tuple[ToolSpec, ...]: ...        # this iteration's selected catalog
    async def call(self, name: str, tool_input: dict[str, Any]) -> ToolResult: ...
```

Rules for nested calls:

| Concern | Rule |
| --- | --- |
| Catalog | Only tools in the **same iteration's selected catalog** (profile, `read_only`, agent authority, MCP generation). Tools not in it raise `NameError` in the sandbox. `codemode` itself is never exposed (no recursion). |
| Hooks | `PreToolUse`/`PostToolUse` fire with the **real** tool name, key, and bundle. A block becomes a sandbox exception whose message is the hook reason. A modify rewrites the input, which is revalidated and re-gated, exactly as in the loop. |
| Permissions | Same `gate.plan`. `ask` goes through the same durable `permission.requested` → UI → `permission.resolved`, with the nested `call_id` and an added `parent_call_id`. Session grants ("allow for session") apply both ways. |
| Unattended `fail_turn` | The invoker records the reason and cancels the code run. The codemode tool returns an error result. `run_turn` then checks the shared `BatchOutcome` and fails the turn with the same `unattended policy fails the turn: …` message. |
| Denied or blocked call | Raised in the sandbox as `PermissionError(<exact redacted message>)`, so code can `try/except` and continue. |
| Tool error (`is_error=True`) | Raised as `ToolError` (a `RuntimeError` subclass, declared in the stubs) with the first text block. |
| Concurrency | A per-codemode-call scheduler reproduces `ToolManager.dispatch` ordering. Mutating or `exclusive` tools take an `asyncio.Lock` and wait for in-flight reads to finish. Read-only tools share an `asyncio.Semaphore(tools.max_parallel)`. `gather` over reads is parallel, and writes are never interleaved. |
| Timeouts / caps | Each nested call keeps its own `ToolSpec.timeout_s` and `max_result_tokens` via `ToolManager._execute` and `cap_result`. Nothing new. |
| Cancellation | Same `CancelToken`. Cancelling the turn cancels in-flight nested calls, then the `feed_run` task, and the Monty session is discarded. |
| Budget | `max_calls` per run (maps to Monty `max_suspensions`, default 200) plus a turn-level counter. Exceeding it raises in the sandbox. |

### 3. Durable log and views

Nested calls are **not** added to the model message IR. The assistant
message holds one `ToolUse(name="codemode")`, and the following user message
holds one `ToolResult`. That keeps every provider's tool_use/tool_result
pairing valid.

Nested calls are durable as **events**, like any call:
`tool.requested`, `permission.*`, `tool.started`, `tool.progress`,
`tool.completed`/`tool.failed`, and `tool.result`. All of them carry
`parent_call_id`. Replaying the log rebuilds the same nested tree, which
satisfies rule 5 ("durable log first").

- `view/model.py`: `ToolCallView.parent_call_id: str | None = None` and
  `child_call_ids: list[str]`, mirroring `child_agent_ids`.
- `view/reduce.py`: `_ensure_tool` and `_on_tool_*` copy `parent_call_id`
  and link children to the parent. `TurnView.tools` stays flat and ordered,
  and renderers group by parent.
- `_emit_tool_results` for nested results is emitted by the invoker (event
  only). The loop's `session.append_message` for the batch is unchanged.
- Tests: fold a recorded codemode turn and compare live vs replayed
  `ConversationView` (`tests/test_view_*` pattern).

### 4. The `codemode` tool

New builtin `nexus/tools/builtin/codemode.py`:

```python
ToolSpec(
    name="codemode", bundle="code", group="Code",
    description=<generated; see below>,
    input_schema={"type": "object", "required": ["code"], "properties": {
        "code": {"type": "string", "maxLength": 20000,
                 "description": "Python (Monty subset). Tools are async functions: await them."}}},
    mutates=False,            # its own effects are none; each nested call is gated on its own
    concurrency="exclusive",  # runs alone in a model batch, so its writes never race sibling calls
    timeout_s=None,           # must not expire while an approval is parked (see limits)
    permission_key=lambda _i: "codemode",
)
```

Run steps:

1. `invoker = ctx.invoke_tool`, `sandbox = ctx.code_sandbox`. If either is
   missing, return an actionable error ("install `nexus[codemode]`").
2. Build `external_lookup` from `invoker.specs()`. Each tool becomes
   `async def <name>(**kwargs)`, which validates that the arguments are
   keyword-only and calls `invoker.call(name, kwargs)`.
3. Convert each `ToolResult` into a Python value: text blocks joined into
   `str`. If a tool declares structured output later (`metrics` or JSON
   text), return it as `dict`. Image blocks become `"[image omitted]"`.
4. `await sandbox.run(code, external_lookup, stubs, limits)`, with prints
   collected into a bounded `CollectString(max_bytes=…)`.
5. The model-visible result has three parts: printed output, the repr of the
   trailing expression (JSON when possible), and a one-line summary
   (`N tool calls, M failed, X ms`). It is capped by
   `ToolSpec.max_result_tokens`. Errors are rendered with
   `MontyRuntimeError.display('traceback')` and are also bounded.
6. `display`/`metrics` carry `calls`, `failed`, `cpu_ms`, and `wall_ms` for
   the UI.

**Stubs and description** (`nexus/codemode/stubs.py`): turn each selected
tool's JSON schema into a typed async signature and docstring:

```python
async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
    """Read a file from the workspace…"""   # first ~300 chars of the description
```

Mapping: string/number/integer/boolean/null → `str`/`float`/`int`/`bool`/
`None`; array → `list[...]`; object → `dict[str, Any]`; enum → `Literal[...]`;
anything else → `Any`. Properties that are not identifiers or are Python
keywords fall back to `**kwargs: Any`. The same text is used in two places:

- as `type_check_stubs`, so `ty` rejects `read(pth="x")` **before any tool
  runs**, and
- in the tool description, so the model knows the functions. It is bounded
  (default 12 k chars). When it would overflow, list names with one-line
  summaries and add a sandbox function `help(name) -> str` that returns one
  full stub.

Prompt guidance (in the description) covers the Monty subset. Supported:
`asyncio.gather`, comprehensions, dataclasses, `json`, `re`. Not available:
`open`, `subprocess`, most third-party imports. It also tells the model to
return a compact value rather than print large blobs.

### 5. Sandbox service (`nexus/codemode/`)

A new L3 manager package, owned by `Runtime` like `voice/`:

| File | Role |
| --- | --- |
| `codemode/sandbox.py` | `CodeSandbox`: a lazily started `AsyncMonty` pool (`min_processes=0`, `max_processes=config`, `max_checkouts_per_worker=50`). `run()` checks out a fresh session per call, applies `ResourceLimits` and `OSPolicy(sleep='system', sleep_system_max=5, timezone=local)`, maps Monty errors to a bounded `CodeRunResult`, and discards the session on cancel or crash. `aclose()` is called from `Runtime.aclose`. |
| `codemode/stubs.py` | Schema → stub/description generation (pure, heavily tested). |
| `codemode/convert.py` | `ToolResult` ↔ sandbox value conversion and exception mapping. |
| `codemode/__init__.py` | `available()`: whether the import succeeds and the `monty` binary resolves. Used by tool selection and Doctor. |

- `ToolContext` gains `code_sandbox: CodeSandboxView | None`, a structural
  protocol in `tools/spec.py` (the same pattern as `outbound_http`), so the
  tools layer never imports `nexus.codemode`.
- `tests/test_layering.py`: add `codemode` to the manager tier.
- Dependency: optional extra `codemode = ["pydantic-monty==1.0.0"]` in
  `pyproject.toml`, pinned like `voice`/`documents`. Without it, the
  `codemode` tool is not selected and Doctor says why.

### 6. Configuration

`config/schema.py`: `ToolsSection.codemode: CodeModeSection`:

```toml
[tools.codemode]
mode = "off"            # "off" | "tool" (codemode next to the normal tools) | "only" (see below)
max_calls = 200         # nested tool calls per run (Monty max_suspensions + our counter)
cpu_s = 30              # max_feed_duration_secs: sandbox execution time only
memory_mb = 256         # max_memory
max_output_chars = 20000
max_processes = 2       # worker pool cap
type_check = true
exclude = []            # tool names never exposed to code (e.g. ["question"])
```

- `mode = "only"` shows the model **just** `codemode` plus `todowrite` and
  `question`. Every other tool is available only as a function inside code.
  Permissions are still per nested call. Selection happens in
  `ToolManager._select_profile`, driven by config, with no change to
  `core/`.
- Add a bundle `"code": ("codemode",)` to `tools/bundles.py`. It is included
  in profiles only when `mode != "off"`. `read_only` profiles keep it,
  because nested mutating tools are already absent from their catalog.
- Default permission for key `codemode` is **allow**. The code itself has no
  authority; each nested call is gated on its own. A user can still write
  `codemode` ask/deny rules, and the hook matcher sees `tool=codemode` with
  `input.code`.
- Settings: add the mode to the existing tools settings pages in **both** the
  TUI and web (autosave, scoped reset). This follows the conventions in
  `ui_support/tui_settings.py` and the web settings dialog.

### 7. UI (TUI and web, same behavior)

- The timeline row for `codemode` shows the code as a Python block
  (Monaspace Argon on web, syntax-highlighted in Textual). Nested calls are
  indented children under it, the same way `child_agent_ids` render subagent
  runs. Each child shows its normal tool row (diffs for `edit`, etc.).
- Collapsed summary: `codemode · 12 calls · 1 failed · 1.4 s`.
- Permission prompts for a nested call say `from codemode` and show the
  parent row. The approval dialog is otherwise unchanged, because it is the
  same `permission.requested` payload.
- The details sidebar lists nested calls under the parent.
- Files: `ui_support/timeline.py` (shared grouping), `ui/tui/timeline.py`,
  `ui/web/js/app.js`. Checks: `tests/playwright_web_check.py` and
  `tests/playwright_tui_check.py` gain a recorded codemode turn fixture.
  Screenshots are verified per the web visual style memory.

### 8. Host and Doctor

- No new protocol command is needed for the core feature. Everything flows
  through existing events and approvals.
- `doctor` gains a `codemode` line: extra installed, binary path resolved,
  pool status, and configured mode. Errors are redacted.
- The `nexus run --json` stream already forwards tool events. Document
  `parent_call_id`.

## Phases

| Phase | Work | Exit criteria |
| --- | --- | --- |
| 0. Spike | Answer the four open questions above: `request_timeout` during a long host await, cancel semantics, binary resolution in `uv tool`/`pipx`, platform wheels. Record the answers in this file. | Written answers. Go/no-go on `request_timeout`. |
| 1. Refactor | Extract `_execute_tool_batch` from `run_turn`. | Full suite green, with no test edits other than imports. |
| 2. Invoker | `ToolInvokerView`, invoker closure, `dispatch(invoke_tool=…)`, adapter wiring, scheduler, `parent_call_id` events, `fail_turn` propagation. Tested with a **fake** code tool (no Monty) that calls `ctx.invoke_tool`. | Parity tests (below) pass. |
| 3. Sandbox + tool | `nexus/codemode/`, `codemode` builtin, stubs, conversion, config, bundle, extra, Doctor. | Tool tests pass with Monty installed. The tool is skipped cleanly when the extra is absent. |
| 4. Views + UI | Reducer and view fields, TUI and web rendering, settings row in both. | Replay-equals-live test. Playwright web and TUI checks. |
| 5. `only` mode + docs | Selection for `mode="only"`, prompt tuning, `README.md`, `EXTENDING.md`, `docs/core.md`, `docs/textual.md`, `docs/web.md`. | Scripted-provider end-to-end in both modes. |

Commits use Conventional Commits (`refactor:` for Phase 1, `feat:` for the rest).

## Tests

Permission and dispatch parity is the most important group. Put it in
`tests/test_codemode_parity.py`, run each case twice (direct model call vs.
from code), and assert the **same** outcome and events:

- fs rule deny outside the write root, allow inside it, and `ask` that
  resolves allow, deny, and "allow for session" (a grant made from code
  applies to a later direct call and vice versa)
- a symlink swapped between plan and execution is refused by
  `_recheck_fs_entry`
- `PreToolUse` block and modify (modified input is re-validated and
  re-gated), and `PostToolUse` sees the result
- unattended `deny` / `allow` / `fail_turn` (the turn fails with the same
  message)
- a `read_only` profile or read-only subagent: `write` is absent from code
  (`NameError`), not merely denied
- MCP tool naming (`mcp__server__tool`) is callable and gated by the MCP rule
- tool timeout and result cap are identical
- cancel during a nested ask releases the approval and cancels the run

Other test files:

- `tests/test_codemode_stubs.py`: schema → stub mapping, keywords, enum,
  nested arrays, bounded description, and the `help()` fallback.
- `tests/test_codemode_sandbox.py`: limits (CPU, memory, `max_calls`), no fs,
  no subprocess, syntax, type, and runtime errors rendered and bounded, a
  crashed worker replaced, and the session discarded after cancel.
- `tests/test_codemode_concurrency.py`: parallel reads under `gather`, writes
  serialized and never overlapping reads, and `max_parallel` respected.
- `tests/test_view_codemode.py`: nesting in the view, and replay equals live.
- `tests/test_layering.py` / `test_ui_layering.py`: the new package placement.
- Tests that need Monty use `pytest.importorskip("pydantic_monty")`. `dev`
  gains `pydantic-monty==1.0.0`, so CI runs them.

## Security notes

- The sandbox has **no mounts, no `os` handler, no network**. Its only
  capability is calling Nexus tools, and every one of those calls goes
  through the gate. This keeps "tool paths are checked by
  `tools/permissions.py`" true.
- Credentials never enter the sandbox. Tool functions are host closures, and
  only JSON-like values cross the boundary.
- Every limit is bounded: code size, CPU time, memory, call count, output
  size, and pool size.
- Monty runs in subprocess workers, so an interpreter crash cannot take down
  the daemon.
- Error text from Monty and from tools is redacted by the existing
  `_safe_message` / host redaction before it crosses the host boundary.

## Later

- **Persistent REPL per session:** keep variables across `codemode` calls by
  storing `AsyncMontySession.dump()` bytes as a durable record after each
  call and `load_session()` on the next. Replay restores it. Needs a size cap
  and a record type.
- **Snapshot-driven execution** (`feed_start` / `resume`): gives each nested
  call's source line (`FunctionSnapshot.position`), which approval prompts
  and the timeline could show.
- Structured tool outputs (`output_schema` on `ToolSpec`), so code gets
  typed dicts instead of strings.
- Code mode for subagents' default profile once v1 is measured.
