# Plan 03: Session names, code mode, and recursive (RLM-style) context access

Status: implemented offline (2026-10-19); not verified live. CM1-CM8, N1, N2 and
C1-C5 are in the tree with offline tests; E1 is partial (an offline scripted
harness is written but not yet run, see `docs/devtools.md`; live provider trials and the RLM live trial
are NOT done), nor are the Ratatui/desktop visual reviews. **Default changed by explicit user
request (2026-10-19):** `[code_mode] enabled` now defaults to `true` without live trials, and
`pydantic-monty==1.1.0` is a base dependency (the `codemode` extra is a compatibility alias).
`expose = "read_only"` and `context = false` are unchanged. Where this plan and the code
differ, the code and `docs/` win: the Monty pool is a per-call helper process, not
daemon-owned (see `docs/codemode-monty-compat.md`, `docs/decisions.md`); sandbox
names are flat, not `ctx.*`; the session name is stored in the row, not logged as
`session.named`. Resolved user decisions: U1 no rename, U2 batch backfill, U3
manager never, U4 external transcripts off, U5/U8 no, U7 `read_only` and no
only-mode. Remaining gaps: images attach by ref only; structured bash/MCP returns
need producers to set metrics (not yet); per-row collapse on desktop not done;
`llm()` spend is per-program, not in the turn tree budget.

Phase 3 of 3. **Requires plan 01** (immutable logical ids, supervision edges,
segments) for the names track and the context namespace. The code-mode core
(§3.3–§3.8) depends on nothing from plans 01/02 and can ship first. Plan 02's
manager uses names for addressing but does **not** get context access by
default.

---

## 1. Executive intent

1. Give every logical session a **unique, human-friendly three-word name**
   (Docker-style, e.g. `brisk-amber-heron`) with a hard uniqueness guarantee.
   The immutable logical ID stays the real key; the name is for people and
   prompts.
2. Add **code mode**: one `code` tool in which the model writes a small Python
   program, run in the **Pydantic Monty** sandbox, that calls Nexus tools
   (built-in and MCP) as `async` functions, runs independent calls
   concurrently, and filters or joins their results. The model sees the
   program's output instead of every intermediate result. Every nested call
   still goes through the normal Nexus tool pipeline (validation, `PathGuard`,
   hooks, permissions, timeouts, result caps) and is shown to the user as a
   nested row with its full input and output.
3. Build **RLM-style context access** on code mode instead of as a separate
   interpreter tool: a `context` namespace of host functions over session
   contexts, plus bounded recursive `llm()` sub-calls. A root may read a
   child's context **only** through a connected **and** authorised supervision
   edge. Never arbitrary cross-session or cross-workspace access.

One sandbox, one adapter, one event family, one UI row type. Context access is
a capability set that code mode is granted, not a second runtime.

### Non-goals

- Full Python. Monty is a subset interpreter, not CPython (see §2.3). No
  arbitrary imports, no filesystem, no network, no package installs inside the
  sandbox. The only side effects are the host functions Nexus supplies.
- Hiding nested work from the user. Nested results are kept out of the
  *model's* context, never out of the user's view or the durable log.
- Replacing direct tools. Direct tools stay; code mode is added beside them
  (§3.4, U7).
- Writing to another session's log, steering it, or editing its context.
- Giving the manager (plan 02) the `context` namespace (U3).
- Replacing compaction. RLM is an on-demand lookup, complementary to plan 01
  handoffs.
- Persisting interpreter heaps or snapshots (§3.7).

---

## 2. Current state and research

### 2.1 Workspace

- Session ids: ≤ 80 chars, `[A-Za-z0-9_-]` (`nexus/session/ids.py`,
  `docs/sessions.md:29`). Child ids `<parent>/sub/<n>` plus SHA-1 suffix
  (`nexus/runtime.py:1804-1849`). No name column exists; `title` is mutable and
  not unique (`docs/sessions.md:44-46`, `:171-198`).
- Child transcripts are already readable by the host through `AgentTranscript`
  (`docs/host.md:42`) and the child's first `context.assembled` request
  (`docs/agents.md:417-419`, `nexus/runtime.py:5315`). A **model** has no such
  access today; the child "does not see the parent conversation", and the
  parent sees only the child's report (`docs/agents.md:339-341`).
- The design idea is "nothing the agent can see is hidden from the user"
  (`docs/README.md:9-11`). Every code-mode call, nested tool call, context read
  and sub-call must therefore be visible as rows with exact inputs and outputs.
- Bounded host projections already exist to copy: tail reads ≤ 512 KiB, pages
  ≤ 200 (`docs/sessions.md:158-160`).
- Tool pipeline (`docs/tools.md`, `docs/loop.md`): `ToolManager.prepare`
  (unknown tool, strict JSON Schema, `PathGuard` canonicalisation) →
  PreToolUse hooks → batch permission plan (`deny → grants → allow → ask →
  mode`) → asks resolved durably (`permission.requested`/`.resolved`) →
  `dispatch` (bounded parallelism, mutating tools exclusive, per-tool timeouts,
  cancel, result capping) → PostToolUse hooks.
- **`ToolContext.invoke_tool` exists but is not wired** (`nexus/tools/spec.py:647`;
  no other reference in `nexus/`). It is the natural seam for nested calls
  (§3.5).
- Tool names already satisfy `[A-Za-z][A-Za-z0-9_]{0,63}`, i.e. they are valid
  Python identifiers; MCP tools are `mcp__<server>__<tool>`.
- MCP servers default to `tool_loading: "search"` with `McpSearch`/`McpCall`
  and a deterministic local ranker (`nexus/mcp/search.py`, ≤ 2,000 tools per
  server, ≤ 64 servers; `docs/extensions.md:225-250`). Code mode reuses that
  ranker for in-sandbox discovery.

### 2.2 RLM (verified by search on 2026-10-17)

- Paper: Alex L. Zhang, Tim Kraska, Omar Khattab, "Recursive Language Models",
  arXiv:2512.24601 (v1 31 Dec 2025, v3 revised 11 May 2026), MIT CSAIL:
  https://arxiv.org/abs/2512.24601
- Core idea from the abstract: treat the long prompt as part of an external
  environment; the LM programmatically examines, decomposes and recursively
  calls itself over snippets. In practice the input is a **variable in a Python
  REPL**.
- Reported results (abstract, v3; not reproduced here): inputs up to two orders
  of magnitude beyond the context window; with GPT-5, median improvement 26 %
  vs compaction, 130 % vs CodeAct with sub-calls, 13 % vs Claude Code, at
  similar cost.
- Reference implementation: https://github.com/alexzhang13/rlm ("supporting
  various sandboxes"), minimal version github.com/alexzhang13/rlm-minimal, blog
  https://alexzhang13.github.io/blog/2025/rlm/. **Not read in detail**: their
  exact sub-call function names and prompts are not copied here.

### 2.3 Pydantic Monty 1.1.0 (spiked 2026-10-18)

The earlier draft assumed 0.0.x and an in-process `Monty(code).run(...)`. Both
assumptions are out of date. PyPI now has `1.0.0` and `1.1.0`. A throwaway venv
(Python 3.13, `pydantic-monty==1.1.0`, macOS arm64) gave these results:

| Area | Finding (1.1.0) |
| --- | --- |
| Packaging | `pydantic-monty` pulls `pydantic-monty-client` and `pydantic-monty-runtime`; the runtime installs a ~27 MB `monty` binary in the venv's `bin/` |
| Execution model | **Subprocess worker pool**, not in-process. `AsyncMonty(max_processes=…, request_timeout=…, checkout_timeout=…, max_checkouts_per_worker=…)` → `async with pool.checkout(limits=…, type_check=…, type_check_stubs=…) as session` → `await session.feed_run(code, inputs=…, external_lookup=…, print_callback=…)`. The worker speaks framed protobuf over stdio (`monty subprocess`) |
| Host functions | `external_lookup={name: fn}`. Callables, **including coroutine functions**, become sandbox functions. Absent names raise `NameError`. Positional and keyword args both work |
| Concurrency | `await asyncio.gather(read('a'), read('b'), bash(command='ls'))` with three 0.2 s host coroutines finished in **0.20 s**, so host calls really overlap |
| REPL state | Globals persist across `feed_run` calls within one checkout (`SAVED = 41` then `SAVED + 1` → 42) |
| Type checking | `checkout(type_check=True, type_check_stubs="async def read(path: str, …) -> …: ...")` rejected `await read(path=3)` **before running**, with a ty-style diagnostic (`error[invalid-argument-type] … Expected str, found Literal[3]`) |
| Limits | `ResourceLimits` keys: `max_feed_duration_secs`, `max_turn_duration_secs`, `max_memory`, `gc_interval`, `max_recursion_depth` (default 1000, cannot be disabled), `max_suspensions` (external calls per checkout, default 1000, cannot be disabled), `max_total_sleep_secs`. The duration clock only runs while sandboxed code executes, not while it waits on the host |
| Limit behaviour | Busy loop → `TimeoutError` at 2.0 s. Memory → `MemoryError` at 64 MiB. Too many host calls → `RuntimeError: suspension limit 3 exceeded`. **After a memory or time limit the session heap is poisoned** (every later feed in that checkout failed with `MemoryError`). The docs say to discard the session, so code mode uses one checkout per call |
| Denials | `open('/etc/passwd')` and `os.listdir('/')` → `PermissionError` (no mounts given); `import subprocess` → `ModuleNotFoundError`; `json`, `re`, `datetime`, `dataclasses`, `asyncio` import fine |
| Syntax | Works: `async`/`await`, comprehensions, f-strings, `try/except`, classes, `@dataclass`, `with`, lambdas. **Missing: `match` statements, generators/`yield`** (`NotImplementedError` from the parser) |
| Errors | `MontyRuntimeError` (sandbox exception; session survives unless a limit poisoned it), `MontyTypingError`, `MontySyntaxError`, `MontyCrashedError` (worker died or hit `request_timeout`; pool replaces the worker) |
| Danger surface | `MontySession.install_dependencies()` and **PEP 723 inline `# /// script` blocks auto-installing on `feed_run`** exist but are documented as "only supported by an embedded-CPython worker". The pure-Monty worker raises `MontyRuntimeError`. Also `mount=`, `os=` (an `OsHandler`) and `OSPolicy`. Nexus never passes these (§3.8). PEP 723 rejection on the pure worker is **not verified** |

Not verified: Linux and Windows wheels, cold-start latency of the pool, worker
RSS, and behaviour under `request_timeout` kills. The project still describes
itself as young; its author reports no successful escape so far, which is not
a guarantee.

### 2.4 Code mode in opencode (read from `references/opencode`, 2026-10-18)

Source: `packages/codemode/README.md`, `packages/core/src/codemode/*.ts`,
`packages/core/src/tool.ts:225-300`; upstream
https://github.com/anomalyco/opencode/blob/dev/packages/codemode/README.md .
Concept credited to Cloudflare (https://blog.cloudflare.com/code-mode/).

- **Own interpreter.** A pure interpreter for a JavaScript-like subset, not a
  sandboxed JS engine: "only runs the language features we implement".
  Unsupported syntax → `UnsupportedSyntax` diagnostic with a location.
- **One `execute` tool**, input `{ code }`. Tools are reached as
  `await tools.<namespace>.<name>(input)`; bracket notation for odd names.
  Without `return`, the last top-level expression is the result.
- **Which tools go inside.** Each tool registration has `codemode` (default
  true). Core tools (`read`, `edit`, `write`, `patch`, `glob`, `grep`, `shell`,
  `webfetch`, `websearch`, `skill`, `question`, `subagent`) set
  `codemode: false` and stay as **direct** provider tools. MCP and namespaced
  `opencode.*` API tools go **inside** code mode only. The name `execute` is
  reserved.
- **Nested calls reuse the pipeline.** Each nested call runs
  `beforeExecute` (permission/repair hook) and then the normal `executeTool`.
  `hooks` `tool.before`/`tool.after` stream progress rows
  (`{tool, status: running|completed|error, input}`) to the UI.
  Files/images returned by nested tools are collected and attached to the
  `execute` result.
- **Catalog.** A system-instruction block (not the tool description) lists
  namespaces and TypeScript signatures under an inline budget of **2,000
  tokens**, with pinned tools first; if partial, an in-sandbox synchronous
  `search(...)` finds the rest. Catalog changes are sent as **diffs**
  ("new tools…", "no longer available…") so the prompt cache is not broken.
- **Results.** Always a `Result`: `ok`, JSON-safe `value`, `warnings`, `logs`,
  `truncated`, and `toolCalls` (admitted calls in order, kept after failure).
  Diagnostic kinds: `ParseError`, `UnsupportedSyntax`, `UnknownTool`,
  `InvalidToolInput/Output`, `ToolCallLimitExceeded`, `TimeoutExceeded`,
  `ToolFailure`, `ExecutionFailure`, `Truncated`.
- **Limits.** `timeoutMs`, `maxToolCalls`, `maxOutputBytes`; no defaults;
  boundary data ≤ 32 nesting levels; unfinished calls interrupted at program
  end.
- MCP servers that are themselves code mode get `codemode: false` so code mode
  is never nested inside code mode.

### 2.5 Code mode in pi (read from `references/pi`, 2026-10-18)

Source: `packages/coding-agent/docs/codemode.md`, `packages/codemode/README.md`,
`packages/coding-agent/src/extensions/codemode/{tool,renderer}.ts`; upstream
https://pi.dev/docs/latest/codemode .

- **QuickJS compiled to WASM**, one worker thread per execution, 256 MB heap,
  at most two live workers per pi process (admission control; a cancelled
  worker holds its slot until it exits).
- **Tool `codemode`**, input is raw JS source (with a Lark grammar for
  grammar-constrained providers), run as an async function body. An optional
  first line `// @options: {"max_output_tokens": …, "timeout_ms": …}`.
- **Globals:** `tools.<name>(args)`, `text()`, `image()`, `console.*`,
  `return`, `exit()`, `store(key, value)`/`load(key)`, `ALL_TOOLS`,
  `searchTools(query)` (BM25), `describeTool(name)`, `describeNamespace(name)`,
  and `models.*` (non-LLM classifiers and image models; **chat models cannot
  be run from scripts**).
- **Every session tool is callable**, including `bash`, `edit`, `write`.
  Nested calls run through `ctx.executeTool`, "so validation,
  `tool_call`/`tool_result` hooks, and permission checks apply exactly as for
  direct calls". Tools with an output schema resolve to structured values
  (`bash` → `{output, truncated, full_output_path?, exit_code, …}`, up to
  1 MiB); others to text; failures reject with an `Error`.
- **Modes:** `codemode.mode = "on"` (default: direct tools stay, plus code mode)
  or `"only"` (direct tools hidden; everything goes through scripts).
  `codemode.inlineBudget` = **3,000 tokens** of declarations; deferred/MCP tools
  are not listed so the description stays stable while servers connect.
- **Durable store:** `store`/`load` writes are kept only when the script
  succeeds, appended as `codemode-store` session entries, so resume and
  branches see the right values. Caps: 256 Ki chars per value, 1 Mi total.
- **Output:** "Script completed/failed", wall time, ordered text/image items,
  one `<console_output>` block; long output keeps head and tail and spills the
  full text to a temp file. Partial output is kept on failure; nested calls
  are not undone.
- **UI:** the call row shows the script; the result row lists nested calls
  with status icons (`… ✓ ✗ ⊘`), args, duration and cost, then the output.
  "Nested calls are not separate tool rows because they never reach the model
  as tool calls."
- Scripts cannot start other `codemode` scripts.

### 2.6 What Nexus takes from each

| Question | opencode | pi | **Nexus (this plan)** |
| --- | --- | --- | --- |
| Language/sandbox | own JS-subset interpreter, in-process | QuickJS-WASM in a worker thread | **Python subset in Monty**, subprocess worker pool |
| Which tools inside | MCP + API only; core tools direct | all tools | allowlist: read-only built-ins + MCP by default; mutating tools opt-in (U7) |
| Direct tools hidden? | core stay direct | `on` (default) / `only` | `on` only in v1 |
| Nested permissions | `beforeExecute` hook | same pipeline as direct | same pipeline: `prepare` → hooks → permission plan → `dispatch` |
| Arg checking | schema decode at call time | schema at call time | **ty type-check against generated stubs before running**, plus schema validation at call time |
| Catalog | system block, 2k tokens, diffs | tool description, 3k tokens, stable | tool description lists namespaces + pinned stubs (stable, cache-friendly); `search_tools()` / `describe_tool()` in sandbox |
| State across calls | none | `store`/`load` as session entries | `store`/`load` as durable events (§3.7) |
| Sub-model calls | no | non-LLM only | `llm()` single tool-less request, bounded (context namespace only) |
| UI | progress rows | nested list in one row | nested rows with **full** input and result, expandable, plus the program |

---

## 3. Target design

### 3.1 Names

Format: `adjective-adjective-noun` (or `adjective-color-animal`), lowercase
ASCII, hyphen-separated, each word 3–10 letters, total ≤ 32 chars. Example:
`quiet-cobalt-otter`.

| Rule | Contract |
| --- | --- |
| Key | the logical id (plan 01) is immutable and is the only foreign key anywhere |
| Uniqueness | `UNIQUE (name)` across the **whole DB** (all projects), because cross-project lists and the manager address sessions by name |
| Generation | CSPRNG (`secrets`) pick from curated word lists; insert in the same `BEGIN IMMEDIATE` as the logical session row; on `UNIQUE` conflict retry up to 16 times; then append `-2`…`-99`; then fail loudly (never reuse) |
| Space | e.g. 512 × 128 × 512 ≈ 33.5 M names. At 100 k sessions, the chance any single draw collides is about 0.3 %; the retry loop makes failure negligible. The DB constraint, not probability, is the guarantee |
| Immutability | a name is assigned once and never reassigned, even after purge (keep a `retired_names` table, or simply never delete the row's name entry) so old references never point to a new session |
| Rename | not supported in v1 (U1). Titles stay the editable label |
| Word lists | reviewed, vendored, no offensive or confusable words; no word combos that look like commands. Bump `NAMES_VERSION` on change (existing names unaffected) |
| Display | dashboard shows `title` with the name as a secondary label; prompts and tools accept name or id |

Schema (part of or after plan 01's v6):

```sql
CREATE TABLE session_names (
  name        TEXT PRIMARY KEY,           -- global uniqueness
  project_id  INTEGER NOT NULL,
  logical_id  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  retired_at  REAL,                       -- set on purge; name never reused
  UNIQUE (project_id, logical_id)
);
```

Event: `session.named` (`name`, `names_version`) as an early record in the
first segment, so replay and export carry it. Backfill: existing sessions get
names lazily on first list/open in a transaction, or in a migration batch (U2).

Retrieval host commands:

| Command | Behavior |
| --- | --- |
| `SessionResolve(ref)` | `ref` = name or id → `{logical_id, project, name, kind, state}`; only sessions the caller's workspace may list; trashed → `trashed: true`; purged → `retired` |
| `SessionList`, `SessionTree`, `ProjectSessionsList` | add `name`; `query` matches names too (literal substring, existing rules `docs/sessions.md:79-83`) |
| CLI | `nexus sessions show <name>`; chat `/open <name>` |

### 3.2 Authorisation for context access

A session **A** may read session **B**'s context only if all hold:

1. `A` and `B` are in the **same project** (edges are project-scoped, plan 01
   §3.2).
2. There is a supervision path from `A` down to `B` using `spawned` edges only
   (A is B's ancestor). Not `forked`, not `managed` (manager excluded, U3), not
   sibling, not upward. The path length ≤ `max_depth`.
3. Each edge on the path has `authority.context_read = true`. Recommended
   default: `true` for `spawned` edges created by a Nexus-run child; `false` for
   external workers (Claude Code / agy: their transcripts are Claude's/agy's own
   format and may include data Nexus never mediated, U4).
4. The `context` namespace is enabled for A's role (§3.9) and the permission
   engine allows `ContextQuery(<target-name>)` (`ask` by default for targets
   other than self).
5. A's own segments are always readable by A (that is the non-recursive base
   case and helps after a plan 01 handoff).

The check runs in the host port on **every** context function call, not once
per program, so an edge that ends or a session that moves to trash mid-program
stops access.

### 3.3 The `code` tool

One built-in tool, `code`, in a new `code` bundle (added to the `coding`
profile only when `[code_mode] enabled = true`). Tool input:

```json
{
  "program": "<python source, ≤ 16 KiB>",
  "purpose": "one line: why this program runs (shown in the row header)",
  "timeout_s": 60,
  "targets": ["self", "quiet-cobalt-otter"]
}
```

- `program` is a Monty snippet. Top-level `await` works. The **last top-level
  expression** is the result (Monty's REPL rule, same as opencode's).
  `print()` output is collected separately.
- `timeout_s` (optional, 1…`[code_mode] max_tool_seconds`, default 60) is the
  wall-clock bound for the whole call, including nested calls.
- `targets` (optional) is used only by the `context` namespace (§3.9). Each
  non-self target triggers a `ContextQuery(<name>)` permission decision
  **before** the program starts, so the user approves once, up front, with the
  program visible.
- `ToolSpec`: `mutates` = true if any callable nested tool mutates (honest
  worst case, so `code` is `exclusive` when mutating tools are exposed;
  read-only exposure keeps it `parallel`). `permission_key` = `code`.
  `max_result_tokens` = the normal 25,000.
- `code` is never callable from inside a program (no nesting; opencode and pi
  both forbid it).

Inside the program, Nexus binds (everything crossing is plain
`str`/`int`/`float`/`bool`/`None`/`list`/`dict`, ≤ 32 nesting levels, never
host objects):

| Name | Kind | Meaning |
| --- | --- | --- |
| `<tool>(**args)` | async external | one per callable tool, named exactly as the Nexus tool (`read`, `grep`, `mcp__github__search_issues`). Keyword args match the JSON Schema properties |
| `search_tools(query, limit=8)` | sync external | ranked `{name, description}` list over callable tools, reusing `nexus/mcp/search.py` ranking |
| `describe_tool(name)` | sync external | the tool's description and generated Python stub, or `None` |
| `store(key, value)` / `load(key)` | sync external | small durable JSON values (§3.7) |
| `context.*`, `llm(...)` | async external | only when the context namespace is granted (§3.9) |

Flat names, not `tools.read(...)`: Monty's `external_lookup` resolves bare
names, Nexus tool names are already identifiers, and flat names match how the
model sees direct tools. MCP server names that are not identifier-safe are
normalised the way pi does (`-` → `_`), with a collision check that fails the
catalog build loudly. (Whether an in-sandbox prelude could offer `tools.x`
namespaces is not verified and not needed.)

Nested tool return values:

- A tool whose `ToolExecutionResult` carries structured data (e.g. `bash` exit
  code and output; MCP `structuredContent`) returns that dict.
- Otherwise the result text as `str`.
- A failed, denied, or invalid call raises `ToolError(message)` in the sandbox
  (catchable with `try/except`), carrying the same model-visible error text a
  direct call would produce.
- Images and files from nested calls are not passed into the sandbox as bytes.
  The program gets a placeholder `{"type": "image", "ref": "img-3"}` and
  returning or printing the ref attaches the image to the `code` result (as
  opencode attaches collected files).

### 3.4 Exposure: which tools a program may call

`[code_mode] expose` decides the callable set; the session's per-tool
selection, agent allowlist, and profile still apply first (a tool disabled for
the session is never callable from a program).

| Value | Callable inside `code` |
| --- | --- |
| `read_only` (**default**) | every selected tool with `mutates = false`: `read`, `glob`, `grep`, `webfetch`, `websearch`, read-only MCP tools |
| `all` | every selected tool, including `bash`, `edit`, `write`, `apply_patch` and mutating MCP tools; each still gated by permissions |
| `[list]` | an explicit allowlist of tool names / `mcp__server__*` globs |

Always excluded: `code`, `subagent`, `question`, `todowrite`, `skill`, the
`meta` bundle, `McpSearch`/`McpCall` (the program uses MCP tools directly and
`search_tools()` for discovery). Rationale: these drive the loop, the user, or
the agent tree, not data, and they need to stay visible as top-level rows.

Direct tools stay as direct tools (pi's `on` mode). An `only` mode that hides
direct tools is a later decision (U7) after evaluation.

The `research` profile keeps its meaning: nested mutating calls are refused,
as `McpCall` refuses mutating targets today.

### 3.5 Nested-call pipeline (wire `ToolContext.invoke_tool`)

The runtime supplies `ToolContext.invoke_tool(name, args, *, parent_call_id)`
to the `code` tool. Each host-function call from the sandbox goes through the
**same** path as a direct call, one call at a time:

1. **Resolve** against the frozen catalog pinned for this iteration (one
   manifest generation per iteration, `docs/loop.md`); unknown → `ToolError`.
2. **Prepare** (`ToolManager.prepare`): strict schema validation,
   permission-key canonicalisation through `PathGuard`.
3. **PreToolUse hooks** with a `parent_call_id` field so hooks can tell nested
   calls apart (and block them).
4. **Permission** via the same engine and grants. An `ask` emits
   `permission.requested` with the nested call's tool, key and args **and the
   parent program**, and awaits a `PermissionResolve` like any other ask. The
   sandbox stays suspended meanwhile; Monty's duration clock does not run while
   suspended (§2.3), but the `timeout_s` wall clock does. No approver attached
   → `on_unattended` as today.
5. **Dispatch** with the tool's own timeout, cancel token, and result cap.
   Concurrency: read-only nested calls may overlap (bounded by
   `tools.max_parallel`); a mutating nested call waits for in-flight nested
   calls and runs alone, matching the manager's `exclusive` rule.
6. **PostToolUse hooks**, then the value goes back into the sandbox.

`ToolManager` must not grow a second dispatch path: the nested call is a
batch of one through the existing `prepare`/`dispatch`. Ask resolution
(hooks, `permission.requested`, awaiting `PermissionResolve`) lives in the loop
today; it moves behind a protocol port that `runtime.py` builds and injects as
`invoke_tool`, so `core/loop.py` still knows only protocols
(`tests/test_layering.py`) and the `code` tool never reaches into the loop or
the `Runtime`. Exact seam to be settled in CM4.

Batch-level invariants that change: a direct batch is approved as a whole
before anything runs. A program's nested calls are approved one by one as the
program reaches them, so a later denial can leave earlier calls done. This is
the same as pi ("calls made before a failure are not undone") and is stated in
the tool description. Mutating exposure (`all`) is off by default for this
reason.

### 3.6 Monty adapter and worker pool

`nexus/tools/codemode/monty_adapter.py` (new, L3 tool support) is the **only**
module that imports `pydantic_monty`:

- Depends on a **pinned** `pydantic-monty==1.1.0` (spiked §2.3); a base
  dependency (the `nexus[codemode]` extra is a compatibility alias). If it is missing
  or the wrong version, `code` is absent from the catalog and
  `nexus doctor` says why. `nexus doctor` also reports the Monty version and
  the `monty` binary path.
- One API used by Nexus:
  `async run(program, *, functions, stubs, limits, deadline, cancel) -> RunResult`.
  All version-specific calls (`AsyncMonty`, `checkout`, `feed_run`, error
  classes) live only here.
- **One `AsyncMonty` pool per daemon**, owned by `Runtime`, started lazily on
  first use and closed on daemon shutdown. `max_processes =
  [code_mode] max_workers` (default 2, like pi), `min_processes = 0` if
  supported (else 1; not verified), `checkout_timeout = 5 s` (busy → the call
  fails fast with "code mode busy, retry", like pi's admission control),
  `max_checkouts_per_worker = 50` (recycle), `request_timeout` = the call's
  `timeout_s` + grace.
- **One checkout per `code` call**; the session is discarded afterwards. No
  REPL state across calls (a poisoned heap after a limit hit, §2.3, cannot
  leak into the next call; replay stays deterministic). Durable state is
  `store`/`load` (§3.7).
- Limits per call (keys verified in §2.3; values proposed):

  | Key | Value |
  | --- | --- |
  | `max_feed_duration_secs` | 10 (CPU-ish time inside the sandbox) |
  | `max_turn_duration_secs` | 5 (between host round trips) |
  | `max_memory` | 64 MiB |
  | `max_recursion_depth` | 200 |
  | `max_suspensions` | 64 (= max nested calls per program, incl. `search_tools`, `store`, context reads) |
  | `max_total_sleep_secs` | 0 |

- **Type check first**: `type_check=True` with stubs generated from the
  callable tools' JSON Schemas (`async def read(path: str, offset: int | None
  = None, limit: int | None = None) -> str: ...`). A `MontyTypingError` is
  returned to the model as a `TypeError` result **before any tool runs**. Stub
  generation lives in `nexus/tools/codemode/stubs.py`; schemas it cannot
  express fall back to `dict[str, Any]` / `Any`, never to rejection.
- `print_callback=CollectString()` (bounded); output > 1 MiB → stop and report
  `limit`.
- Cancellation: the turn cancel token (`nexus/core/cancel.py` view) cancels
  in-flight nested calls, then the feed; if the worker does not stop within
  the grace, it is killed and replaced (`MontyCrashedError` path).

### 3.7 Durable store

`store(key, value)` / `load(key)` give a program small JSON state across `code`
calls in one session (IDs, cursors, digests), as pi does.

- Writes are buffered during the program and committed **only if the program
  succeeds**, as one `code.store` event (`call_id`, `set`, `delete`).
- `load` reads the reduction of `code.store` events in the session (and in
  plan 01 segments, the current lineage), so resume, replay and export carry
  the values. No interpreter snapshot is ever persisted (avoids deserialising
  stored bytes into the sandbox).
- Caps: key ≤ 128 chars; value ≤ 64 KiB JSON; total ≤ 512 KiB per session
  (smaller than pi's, because Nexus also shows the store in the context view).
- The store is shown in the session's context inspection as a labelled table.

### 3.8 Hard sandbox rules (adapter-enforced, tested)

- Never pass `mount=`, `os=`, `cwd=`, or a non-default `os_policy`; never call
  `install_dependencies`, `dump`, `load_session`, or `load_snapshot`.
- Reject programs containing a PEP 723 `# /// script` block before feeding
  them (defence in depth; §2.3 says the pure worker refuses installs, not
  verified).
- Assert at pool start that the worker is the pure-Monty sandbox, not an
  embedded-CPython worker (e.g. `import subprocess` must fail; exact probe
  chosen in CM2).
- Only JSON-like values cross; host callables are wrappers that convert and
  bound arguments and results; host exceptions become `ToolError` with
  redacted text (`redact_secrets`).
- The worker subprocess inherits a scrubbed environment (no credentials, no
  provider keys) and runs with the daemon's cwd set to a private empty temp
  dir. Whether Monty's runtime honours a custom env is not verified (CM2).

### 3.9 The `context` namespace (RLM)

When `[code_mode] context = true` and the session's role is allowed (roots in
v1, U3), the program additionally gets:

| Name | Kind | Meaning |
| --- | --- | --- |
| `targets` | input | list of the authorised target names (from the `targets` tool input, after permission) |
| `context.meta(t)` | external | `{name, kind, segments, records, tokens_estimate, status}` |
| `context.messages(t, start, stop)` | external | list of `{seq, role, text}` (text blocks only; thinking omitted unless already user-visible; tool results clipped to 8,000 chars) |
| `context.tool_calls(t, start, stop)` | external | list of `{seq, tool, input_summary, is_error, result_excerpt}` |
| `context.search(t, needle, limit)` | external | literal substring hits with `seq` and a 300-char window; ≤ 50 hits |
| `context.report(t)` | external | the child's final `SubagentOutcome.render()` or handoff digest |
| `llm(prompt, text)` | external | **recursive sub-call** (below) |

`context.x` names: if Monty cannot resolve dotted names through
`external_lookup` (not verified), they become flat `context_meta(...)` etc.
CM8 decides; the tool description shows whichever form is real.

Design choice: contexts are exposed as **lazy accessor functions**, not as one
giant string variable. The RLM paper puts the whole prompt in a variable; doing
that literally would copy up to MBs into the sandbox per call and bypass
per-read authorisation. Accessors give the same "context as environment"
pattern with per-read checks (§3.2), bounds and a visible audit trail.
(Inference, not from the paper.) Each read is a nested call with permission
key `ContextQuery(<target-name>)`, so it appears in the same nested-call rows
and uses the same grant rules as tool calls.

Recursive sub-call `llm(prompt, text)`:

- Runs a **single, tool-less** model request (no loop, no tools, no session),
  like the auto-titler (`docs/sessions.md:179-182`), on the **calling
  session's own model** by default. No silent upgrade; a different model only
  if the user configured `[code_mode] sub_model` explicitly.
- Bounds per `code` call: `max_sub_calls = 16`, `max_sub_input_tokens = 32k`
  each, `max_total_sub_tokens = 200k`, `max_depth = 1` (a sub-call cannot run
  a program or call `llm` itself in v1; deeper recursion is U5).
- Each sub-call is a visible nested row with model, token counts and a bounded
  excerpt of prompt/answer. Usage is charged to the calling session and to the
  shared `SubagentBudget` token/cost budget (`docs/agents.md:364-366`).
- `text` passed to the sub-model is wrapped as data
  (`<context source=... seq=...>`), and the sub-model output is returned as
  data to the program, never executed.

`llm()` is only bound together with the context namespace in v1. Offering it
to plain code mode (e.g. to summarise many `grep` hits) is U8.

### 3.10 Result format (what the model sees)

```
Program completed in 1.84 s · 6 nested calls (5 ok, 1 error) · 2 sub-calls

<final value: str as-is, other values as indented JSON>

Printed output:
<print() lines, bounded>

Nested calls:
1. grep {"pattern": "TODO", "path": "nexus/"} → ok, 214 lines (12.3 KiB)
2. read {"path": "nexus/tools/spec.py", "offset": 600} → ok, 80 lines
3. read {"path": "/etc/hosts"} → denied: outside read roots
...
```

- On failure: `Program failed` plus the diagnostic kind (`SyntaxError`,
  `TypeError` (from the type check), `ToolError`, `NameError`, `limit:
  memory|time|calls|output`, `cancelled`, `crashed`), the Monty traceback with
  line numbers, and whatever value/print output exists. Nested calls already
  done are listed, as opencode and pi do.
- The nested-call appendix is a one-line summary per call (tool, compact args,
  status, size). It tells the model what happened without re-adding the
  results; the user sees full results in the UI.
- The whole result obeys `tools.max_result_tokens` (25,000); clipping is
  announced (`docs/context.md:111-112`). The `context_note` used after
  eviction is `code: <purpose> → <status>, N calls`.

### 3.11 Catalog presentation

- The `code` tool description holds the fixed instructions (Python subset
  rules: no `match`, no generators, no imports beyond `json`/`re`/`datetime`/
  `asyncio`/`dataclasses` (list to be fixed in CM1), use `asyncio.gather` for
  independent calls, last expression is the result), followed by the callable
  catalog: one line per namespace (`built-in`, `mcp:<server>`) with counts,
  and full stubs for **pinned** tools within an inline budget of 2,500
  estimated tokens (between opencode's 2,000 and pi's 3,000).
- Tools that do not fit, and all search-loaded MCP tools, are found with
  `search_tools()` / `describe_tool()`. This keeps the description stable while
  MCP servers connect (pi's reason) and keeps the provider prompt cache warm.
- The catalog is frozen per iteration with the manifest pin. A change between
  iterations re-renders the description (Nexus already re-sends tool schemas
  per request; diff-style updates like opencode's are not needed in v1).
- Context inspection shows the `code` tool row with its full description, the
  generated stubs for every callable tool, and the exposure setting.

### 3.12 Events

One family, `code.*`, for code mode and the context namespace (replaces the
earlier `context_query.*` draft):

| Event | Data |
| --- | --- |
| `code.started` | `call_id` (the outer `code` tool call), `program_digest`, `purpose`, `exposure`, `callable` (names), `targets` (names + logical ids), `monty_version`, `limits` |
| `code.call` | `call_id`, `nested_id` (`<call_id>.<n>`), `kind` (`tool`/`context`/`llm`/`search`/`store`), `tool`, `args` (full, redacted), `permission` (decision + rule), `status` (`running`/`ok`/`error`/`denied`/`cancelled`), `duration_ms`, `result` (bounded, same capping as a direct result), `bytes`, for `llm`: `model`, `input_tokens`, `output_tokens` |
| `code.store` | `call_id`, `set`, `delete` (only on success) |
| `code.finished` | `call_id`, `status` (`ok`/`error`/`limit`/`denied`/`cancelled`/`crashed`), `limit` hit, `diagnostic`, totals (calls, sub-tokens, wall time) |

- The ordinary `tool.*` events and the persisted `ToolUse`/`ToolResult`
  message blocks exist **only for the outer `code` call**. Nested calls are
  not message blocks, so they never enter the model context and the reducer's
  ToolUse/ToolResult pairing is untouched.
- `code.call` is emitted at start (`running`) and at end with the same
  `nested_id`; the reducer folds them into one nested row under the outer tool
  row in `ConversationView` (`nexus/view/reduce.py`). Replay reproduces it
  (durable log first).
- Nested permission asks use the existing `permission.requested`/`.resolved`
  with an added `parent_call_id`.
- Relayed for children like other tool events.

### 3.13 Surfaces

Per `docs/surfaces.md`, Ratatui and the GPUI desktop client render the same
structure; the web client is deprecated and gets only the generic fallback
(nested `code.call` rows as labelled key/value rows, no new features).

- **Call row:** `code · <purpose>` header, the program in a code block with
  line numbers (Monty tracebacks cite them), `timeout_s`, targets.
- **Nested rows** under it, in start order, each: status glyph, tool name,
  compact args, duration, size; expanding shows the **full args and full
  result** using the same `ui_support/tool_details.py` renderer as a direct
  call of that tool. Denied rows show the matching rule. `llm` rows show
  model, tokens and the bounded prompt/answer excerpts.
- **Result:** the value, printed output, diagnostics; store writes as a small
  table.
- Live progress while the program runs (`code.call running` rows appear as
  they start).
- Approval prompts for nested asks show the parent program with the calling
  line highlighted where the position is known (Monty `FunctionSnapshot.position`
  exists in the low-level API; whether `feed_run` exposes it to callbacks is
  not verified).

---

## 4. Milestones

Two tracks. **N** (names) needs plan 01. **CM** (code mode) needs nothing
until CM8.

| # | Work package | Depends on | State |
| --- | --- | --- | --- |
| CM1 | **Finish the Monty spike** (§2.3 covered the core on macOS): Linux wheel, pool cold-start time and worker RSS, `min_processes=0`, custom env for workers, `request_timeout` kill path, dotted names via `external_lookup`, PEP 723 refusal on the pure worker, callback access to call position, full list of importable stdlib modules. Write `docs/codemode-monty-compat.md`. **Abort criteria:** no Linux wheel, or worker env cannot be scrubbed → revisit (fallback: run the pool under a separate low-privilege helper) | — | done (helper design; Linux, cold start, callback positions not verified) |
| CM2 | Monty adapter + daemon-owned pool + hard rules (§3.6, §3.8); `nexus[codemode]` extra; Doctor row | CM1 | done (per-call helper, not daemon pool) |
| CM3 | Stub generator from JSON Schema; catalog rendering with budget; `search_tools`/`describe_tool` over the existing MCP ranker | CM2 | done |
| CM4 | Wire `ToolContext.invoke_tool`: nested `prepare` → hooks → permission port → `dispatch`; `parent_call_id` on hooks and permission events; layering-safe port injected by `runtime.py` | CM2 | done |
| CM5 | `code` tool with `expose = read_only`; result format (§3.10); `code.*` events; reducer folding + replay fixtures | CM3, CM4 | done |
| CM6 | Ratatui and desktop rendering (§3.13); PTY check and desktop screenshot check | CM5 | partial (rendering written; visual review not done) |
| CM7 | `store`/`load` with `code.store` events and context-view table | CM5 | done |
| CM8 | `expose = all` and lists; mutating nested calls exclusive and gated; `research` refusal | CM5 | done |
| N1 | Names: word lists, generator, `session_names` table, `session.named`, `SessionResolve`, list/query integration | plan 01 M2 | done (name in row, no `session.named` at create) |
| N2 | Name UX: Ratatui and desktop labels, `/open <name>`, CLI `sessions show` | N1 | partial (`/open`, CLI done; visual review not done) |
| C1 | Authorisation function `may_read(A, B)` over edges, with exhaustive tests (siblings, forks, manager, cross-project, trashed, ended edges) | plan 01 M3 | done |
| C2 | Host-side read port: `meta/messages/tool_calls/search/report` over segments, bounded and redacted | C1, plan 01 M7 | done |
| C3 | `context` namespace in code mode for `self` only | CM5, C2 | done |
| C4 | Child targets behind `ContextQuery(<name>)` (`ask`), up-front via `targets` | C3, N1 | done |
| C5 | `llm()` sub-calls with budgets and `code.call kind=llm` | C4 | done |
| E1 | Evaluation: offline benchmark with `ScriptedProvider` (programs as scripted tool calls) for code mode vs direct tools on fan-out tasks (many greps/reads, MCP list-and-filter); one live trial per supported provider; one RLM trial on a long child transcript. Decide default enablement, default exposure, and U7/U8 | CM8, C5 | partial: offline harness written but NOT yet run or verified (`benchmark/codemode_eval.py`, `tests/test_codemode_eval.py`, `docs/devtools.md`); live trials and the decisions are NOT done |

---

## 5. Migration, compatibility, rollback

- Names: additive table; old clients ignore the field. Rollback = stop showing
  names; never delete the table (names must stay retired).
- Code mode is on by default (`[code_mode] enabled = true`; set `false` to
  disable) and Monty is a base dependency. With it off or Monty missing, nothing changes: no tool, no
  catalog text, no pool. Removing it leaves only `code.*` events in logs,
  which the reducer folds as generic rows.
- Monty upgrade policy: pin the exact version; upgrade only with the adapter
  test suite passing on all release platforms; the version is recorded in
  every `code.started`.
- Config keys (to be added to `docs/config.md` in CM2):

  | Key | Default |
  | --- | --- |
  | `code_mode.enabled` | `false` |
  | `code_mode.expose` | `"read_only"` |
  | `code_mode.max_workers` | `2` |
  | `code_mode.max_tool_seconds` | `120` |
  | `code_mode.max_nested_calls` | `64` |
  | `code_mode.inline_budget_tokens` | `2500` |
  | `code_mode.context` | `false` |
  | `code_mode.sub_model` | unset (session's own model) |

---

## 6. Security, budget, observability

Threat model:

| Threat | Mitigation |
| --- | --- |
| Sandbox escape from model-written code | Monty deny-by-default; separate worker **process** (1.x); no mounts/OS handler/deps; scrubbed env; pure-worker probe; pinned version; host functions are the only capabilities |
| Nested call bypassing permissions | nested calls use the same `prepare` (`PathGuard`), hooks, permission engine and grants as direct calls; `deny` stays absolute; no "approve the program" shortcut that approves its calls |
| Approval fatigue / partial effects | default exposure is read-only; mutating exposure opt-in; nested asks show the parent program; the tool description states that done calls are not undone |
| Reading sessions without authority | `may_read` per context call over project-scoped `spawned` edges; `ContextQuery(<name>)` `ask`; manager excluded |
| Cross-workspace leakage | edges never cross `project_id`; `SessionResolve` limited to listable sessions |
| Prompt injection from tool or child content | results returned to the program as data; nothing is executed from results; sub-call output never executed; context text wrapped with source labels |
| Secrets in transcripts or results | `redact_secrets` on every value before it enters the sandbox and on every event; credential paths already blocked from tools |
| Cost blow-up via recursion | `max_suspensions` cap on nested calls; sub-call count/token caps; shared tree budget; depth 1; `code` not callable from programs |
| Resource DoS | Monty memory/time/stack limits; per-call wall bound; pool size cap with fast-fail admission; cancel token; worker recycling |

Observability: every nested call, read and sub-call is a `code.call` event;
`LogsRead` gets names only; Doctor reports the Monty version, binary path, pool
state, or why code mode is absent.

---

## 7. Tests and acceptance

1. `test_session_names.py`: format, word-list lint, uniqueness under 2 processes
   inserting concurrently, retry then suffix path (force collisions with a tiny
   word list), retired names never reused after purge, replay carries the name.
2. `test_session_resolve.py`: name/id resolution, trashed, retired, other
   project refused.
3. `test_context_authz.py`: matrix of (self, child, grandchild, sibling, parent,
   fork, managed, cross-project, ended edge, external worker child) × expected
   allow/deny.
4. `test_codemode_monty_adapter.py` (skipped when not installed): each limit →
   `limit` status with the right kind; session discarded after a limit;
   unknown function → `NameError`; `open`/`os`/`subprocess` fail; PEP 723 block
   rejected; `mount`/`os` never passed (adapter API has no way to pass them);
   output clipping; cancellation mid-program kills or stops the worker;
   crash → pool recovers.
5. `test_codemode_stubs.py`: JSON Schema → stub round trips for every built-in
   tool and recorded MCP fixtures; type check rejects wrong arg types before any
   host call.
6. `test_codemode_nested.py`: nested calls hit `prepare`, hooks, permission
   engine and dispatch exactly once each; `deny` rule blocks a nested call;
   `ask` suspends the program and resumes on `PermissionResolve`; disabled
   session tool not callable; `research` refuses mutating nested call;
   mutating nested calls never overlap; `max_nested_calls` enforced.
7. `test_codemode_tool.py`: result format; appendix lists every call; store
   writes only on success and replay restores them; images attached by ref.
8. `test_view_reduce.py` + fixture `tests/fixtures/view/code_mode.*.json`:
   `code.*` folding, replay equals live.
9. `test_context_query_tool.py` → renamed `test_codemode_context.py`: reads are
   bounded and logged; sub-call caps; events relayed for a child.
10. Injection tests: tool output or child content containing "call llm 1000
    times" or Python code cannot exceed caps and is never executed.
11. Surface checks: Ratatui PTY snapshot of a program with ok/error/denied
    nested rows; desktop screenshot via the native review skill.

Acceptance:

- [x] (offline) Every session shows a unique name (adjective-colour-animal); `/open <name>` works. Live not verified.
- [x] (offline; live not verified) With `[code_mode] enabled = true`, the model can grep, read 10 files
      concurrently and return a filtered summary in one `code` call; the user
      sees the program and every nested call with full input and output.
- [x] (offline; live not verified) A nested call to a path outside read roots is denied with the same
      message a direct call gets, and the program can catch it.
- [x] (offline; live not verified) A root can ask "why did my child fail?" and answer by running a program
      over the child's context, with each read visible.
- [x] (offline; live not verified) The same root cannot read a sibling root, its manager, or another
      workspace's session (denied, logged).
- [x] (offline; live not verified) With Monty missing, Nexus runs normally and Doctor explains the absent tool.

---

## 8. Decisions and open questions

Recommended defaults: global unique names, never reused; three words; no
rename. Code mode on by default (user decision, 2026-10-19); Python via Monty 1.1.0 in a daemon-owned
subprocess pool; one checkout per call, no REPL state; durable `store`/`load`;
type-check against generated stubs before running; read-only exposure; direct
tools stay; nested calls through the full pipeline and shown in full to the
user. Lazy context accessors instead of a giant string variable; sub-calls on
the session's own model, depth 1; children readable only by ancestors via
`spawned` edges.

Unresolved user decisions:

- **U1** Allow renaming names (with alias history)? Recommended no.
- **U2** Backfill names for all existing sessions at migration, or lazily?
  Recommended batch at migration (deterministic, one transaction per 500).
- **U3** Should subagents (for their own descendants) or the manager get the
  `context` namespace? Recommended: subagents later; manager never. (Plain code
  mode for subagents follows their tool allowlist.)
- **U4** May roots read external-worker (Claude Code/agy) transcripts?
  Recommended off by default.
- **U5** Deeper recursion (sub-calls that run their own programs, as in the
  RLM paper)? Recommended only after E1.
- **U6** ~~In-process Monty vs subprocess isolation~~ — resolved by Monty 1.x:
  workers are always subprocesses. Remaining question: run the pool under a
  separate low-privilege user/sandbox profile (macOS `sandbox-exec`, Linux
  seccomp/landlock)? Recommended: not in v1; revisit after CM1.
- **U7** Code-mode exposure and an `only` mode (pi) that hides direct tools.
  Recommended: `read_only` exposure, no `only` mode, until E1 shows the gain.
- **U8** Offer `llm()` in plain code mode (not only with the context
  namespace)? Recommended no in v1: it turns every program into a cost
  multiplier.
- **U9** Should `code` also accept a grammar constraint for providers that
  support one (pi ships a Lark grammar)? Recommended no; the type check covers
  most of the benefit.

Risks: Monty API churn (adapter + pin; the 0.0.x → 1.x jump already broke the
earlier draft's assumptions); subset Python confusing the model (no `match`,
no generators; the tool description lists the supported subset from CM1, and
diagnostics are clear); the ~27 MB runtime binary as an optional dependency;
partial side effects when mutating exposure is on; code-mode gains reported by
Cloudflare/opencode/pi are for MCP-heavy and fan-out workloads and are
unverified for Nexus; RLM gains in the paper are on long-context benchmarks,
not on agent transcripts (unverified for Nexus).

## 9. References

Searched 2026-10-17:

- Zhang, Kraska, Khattab, Recursive Language Models, arXiv:2512.24601: https://arxiv.org/abs/2512.24601
- Reference implementation: https://github.com/alexzhang13/rlm ; blog: https://alexzhang13.github.io/blog/2025/rlm/
- Pydantic Monty: https://github.com/pydantic/monty ; https://pydantic.dev/monty ;
  https://pydantic.dev/articles/pydantic-monty ; threat list:
  https://github.com/pydantic/monty/blob/main/CLAUDE.md ; releases:
  https://github.com/pydantic/monty/releases
- Docker names: https://pkg.go.dev/github.com/moby/moby/v2/pkg/namesgenerator

Read or run 2026-10-18:

- `pydantic-monty==1.1.0` from PyPI, installed in a throwaway venv; API from
  its `_monty.pyi`/`__init__.py` stubs and a smoke script (§2.3).
- opencode code mode: `references/opencode/packages/codemode/README.md`,
  `interpreter-support.md`, `packages/core/src/codemode/{tool,catalog,instructions}.ts`,
  `packages/core/src/tool.ts`, `packages/core/src/plugin/mcp-codemode-defaults.ts`;
  upstream https://github.com/anomalyco/opencode/blob/dev/packages/codemode/README.md
- pi code mode: `references/pi/packages/coding-agent/docs/codemode.md`,
  `references/pi/packages/codemode/README.md`,
  `references/pi/packages/coding-agent/src/extensions/codemode/{tool,renderer}.ts`,
  `references/pi/packages/coding-agent/docs/settings.md`; upstream
  https://pi.dev/docs/latest/codemode (the local copy was read; the web page
  itself was not fetched)
- Cloudflare's original post, cited by opencode: https://blog.cloudflare.com/code-mode/ (not read)

Not verified: see the "not verified" notes in §2.3, §3.3, §3.6, §3.8, §3.9 and
§3.13. CM1 must settle them before code depends on them.
