# Plan: Code mode as a persistent kernel

Status: implemented (2026-10-24), phases 0–5; live trial (phase 6) not verified. Replaces the
one-shot `code` tool (plan 03, `nexus/tools/codemode/`, [docs/tools.md](../../docs/tools.md#code-mode-the-code-tool)).

## 1. Intent

Today code mode is one tool, `code`, that runs one throwaway Monty program. Its
description carries the full callable catalog (~2500 tokens of stubs) on every
request whether or not the model uses it, the model cannot choose which MCP
servers it wants, and nothing survives between programs except the small JSON
`store`.

This plan turns code mode into a **kernel the model opens, talks to and inspects**:

```
model ── code_start(servers=["github","linear"], purpose="triage open issues") ──▶
      ◀── manual + typed stubs for the built-ins and the github and linear tools

model ── code_run("issues = await github_list_issues(repo='x')\nlen(issues)") ──▶
      ◀── In [1] … Out [1]: 42            (every nested call listed under the cell)

model ── code_run("issues[0]") ──▶                  # look at a variable later
      ◀── Out [2]: {'id': 17, 'title': '…'}         (bounded; clipping announced)

model ── code_attach(servers=["slack"]) ──▶         # more tools, same variables
      ◀── stubs for the slack tools
```

- **Start is a request; the manual is the reply.** The tool list stays small and
  stable (better prompt caching), the manual is paid for only when used, and it
  sits in context where the user can read exactly what the model was told.
- **The kernel persists.** Variables, functions and imports live across cells.
  A cell whose last line is an expression shows that value, like IPython's
  `Out[n]`. A cell that is just `x` is how the model looks at `x`.
- **Optional but preferred.** Direct tools stay available beside code mode. The
  tool descriptions and agent guidance steer multi-step, fan-out or
  filter-heavy tool work towards the kernel.
- **Durable.** Cells are events in the session log. The kernel's state is
  checkpointed after each successful cell and restored after a daemon restart or
  an idle eviction. When it can't be restored, the model is told so explicitly.

### Non-goals

- A real CPython / IPython / Jupyter kernel. The backend is Monty's REPL session
  (decision 1). No third-party packages (`install_dependencies` is only for an
  embedded-CPython worker).
- Sharing a kernel between sessions or between a parent and its subagents.
- Re-running cells automatically to rebuild state. Cells may have side effects.
- Web client or desktop work. The web app is to be deprecated and desktop is
  paused; both keep the generic tool-row fallback.
- Keeping the old `code` tool (decision 5: it is removed).

## 2. What already exists

| Piece | Where | Reused how |
| --- | --- | --- |
| Credential-free helper process, id-correlated host callbacks | `tools/codemode/runner.py`, `monty_adapter.py`, `ipc.py` | Becomes the long-lived **kernel host** (§3.3) |
| Authority-intersected callable catalog, skip list | `tools/codemode/catalog.py` | Per-cell `external_lookup` (§3.6); per-server stub rendering |
| JSON Schema → typed stubs | `tools/codemode/stubs.py` | Manual and `code_attach` replies |
| Nested pipeline (validation, PathGuard, hooks, permissions, caps) | `core/loop.py` `_NestedPipeline`, `ctx.invoke_tool` | Unchanged; cells call it as programs do today |
| `code.call` audit rows, result appendix, image refs | `tools/codemode/tool.py` (`audited`, `CallRecord`, `_appendix`, `_render`) | Moves to `cell.py`; same semantics |
| Context namespace (`context_*`, `llm`, `targets`) | `tools/codemode/context_ns.py` | Bound per kernel; `targets` move to `code_start` / `code_attach` |
| MCP search index | `mcp/search.py` (`SearchIndex`) | `search_tools` in the kernel and `code_start(query=…)` |
| `code.*` reducer folding, `tool_details._code_sections` | `view/reduce.py`, `ui_support/tool_details.py` | Extended for cells; old `code` rows still render from history |
| `[code_mode]` config | `config/schema.py` `CodeModeSection` | New keys (§3.12); `expose` and `pinned` stay |
| Registration per iteration manager | `runtime.py` (`_code_runner`, `register`) | Registers the four tools; kernels outlive turns (§3.4) |

### Phase 0 probe results (resolved; Monty 1.1.0, macOS, 2026-10-24)

The live-session spike is recorded in [docs/codemode-monty-compat.md](../../docs/codemode-monty-compat.md#kernel-plan-phase-0-live-session-spike-2026-10-24). Its helper-scrubbing and fresh-pool/fresh-helper snapshot-restore gates passed on macOS with Monty 1.1.0. Linux execution and snapshot compatibility across patch versions remain unverified.

`AsyncMonty()` → `pool.checkout()` → `AsyncMontySession`:

- `feed_run(code, inputs=, external_lookup=, print_callback=, …)` keeps state
  across calls. The **last expression's value is returned** (`a` → `{'v': 42}`),
  and an assignment-only cell returns `None`.
- Async external functions passed through `external_lookup` are awaited inside a
  cell (`a = await tool(21)` worked).
- `print_callback(stream, text)` receives stdout.
- A raised error (`1/0` → `MontyRuntimeError: ZeroDivisionError…`) leaves the
  session usable; the next cell saw earlier variables.
- `dump()` → bytes. `load_session(bytes)` on a **fresh** checkout restored
  variables, a `def` function and an `import json`. A class instance came back
  to the host as `MontyClassProxy(name='P', attributes={})`.
- Dump sizes: about 1 KB for a few small values, about 670 KB with
  `list(range(100000))`.
- The stub docs say snapshots are **not authenticated**: "Only load unmodified
  bytes from a trusted, compatible Monty producer."

Phase 0 results (full procedure, measurements, and caveats are in the linked
compatibility record above):

- Failed cells are **not transactional**: rebinding, new globals, and mutations
  made before an exception persist; the session remains usable.
- Top-level `locals()` returns the session namespace across cells. `globals()`,
  `dir()`, `dir(before)`, and `vars()` raised `NameError`; do not assume those
  CPython inspection helpers exist. Function-local behavior and bounded/safe
  copying of arbitrary values were not established.
- `checkout(limits=..., type_check=True, type_check_stubs=...)` worked for the
  tested options; timeout and memory limits triggered, and a follow-up cell
  worked. The tested stub enforced the declared argument type. These probes do
  not establish total RSS bounds or exhaustive enforcement of all options.
- One minimal helper plus worker measured roughly 41 MiB combined RSS, and
  already-running pool creation/checkout were measured; these are not product
  capacity or cold-start guarantees.
- On the tested macOS build, the Monty child exposed no environment keys, even
  when the helper had a synthetic marker (positive control) or minimal locale
  variables. The scrubbed helper also launched a working worker. This is an OS
  observation, not a portable guarantee; retain helper scrubbing as defense in
  depth.
- Dump/load succeeded in a new checkout, a fresh pool, and a separate helper.
  Snapshots are version-specific; only Monty 1.1.0 was available, so
  cross-patch compatibility remains unverified. Snapshots are trusted,
  unauthenticated bytes.

The probe did not establish Linux behavior. See the linked record for exact
commands, tested values, and remaining compatibility limits.

## 3. Design

### 3.1 The four tools

All four are coding-profile tools registered by the runtime, never callable from
inside a cell (`catalog.EXCLUDED`), and each is its own labelled timeline row.

| Tool | Input | Does | Mutates |
| --- | --- | --- | --- |
| `code_start` | `servers?: list[str]` (MCP server names, ≤ 32), `tools?: list[str]` (names or `mcp__server__*` globs, ≤ 256), `query?: str` (search terms, ≤ 256 chars), `targets?: list[str]` (session names or ids for context reads, ≤ 50), `purpose: str` (≤ 512) | Starts this agent's kernel, or **re-describes a running one without touching its state**. Returns the manual (§3.2) | no (the kernel only reaches tools through the pipeline) |
| `code_attach` | the same `servers` / `tools` / `query` / `targets` | Adds tools or targets to the running kernel and returns stubs only for what is new | no |
| `code_run` | `cell: str` (≤ 16 KiB), `purpose: str` (≤ 512), `timeout_s?` (≤ `max_tool_seconds`) | Runs one cell (§3.5) | permission key `code`; `exclusive` when mutating tools are attached (as today) |
| `code_reset` | `stop?: bool` (default false), `purpose` | Clears all variables. With `stop`, frees the kernel entirely; a later `code_run` needs `code_start` again | no |

`code_run` without a started kernel fails with `KernelNotStarted: call
code_start first` rather than starting one implicitly. The handshake is what puts
the manual in context.

`code_start` on a running kernel returns the manual again, with the attachments
and variables listed (§3.2). This is the recovery path after compaction has
summarized the original manual away.

Tool descriptions are short (≤ 600 chars each). `code_start` says what code mode
is for and that it is preferred for multi-step or fan-out tool work.

### 3.2 The manual

The `code_start` result is plain labelled text, not JSON:

1. **How it works.** Cells, persistence, `Out [n]`, `await` for every tool,
   `asyncio.gather` for fan-out, results bounded at N chars, and "look at a
   variable by running a cell with just its name".
2. **Language.** The allowed and denied imports and features, moved from the
   current description (docs/tools.md "Language").
3. **Kernel helpers.** `search_tools`, `describe_tool`, `whos`, `ToolError`, and
   the `context_*` / `llm` / `targets` functions when context is on.
4. **Attached tools.** Typed stubs for: the built-in callable tools (by
   `[code_mode] expose`, `pinned` first), then each requested server's tools
   (grouped and headed by server name), then the explicit `tools`, then the top
   `query` matches. All of it is bounded by `manual_budget_tokens` (default
   6000). Anything over the budget is listed by name with "use
   `describe_tool(name)`", and the clip is announced with counts.
5. **Not attached.** Requested names that are unknown, disabled for the session,
   refused by `expose`, excluded, or unsafe identifiers, each with its reason.
   This reuses `Catalog.skipped`.
6. **State** (re-describe only): attachments, cell count, and the `whos()`
   table.

`servers` resolve against the session's **enabled** MCP servers. An unknown or
disabled server goes in "Not attached" with its reason; no error is raised.

### 3.3 Backend: one long-lived kernel host

Contract I3 (credentials never enter the sandbox) stays. The daemon does not
create `AsyncMonty` itself. Instead, `Runner` becomes `KernelHost`: one
long-lived helper process per daemon, started with the scrubbed environment the
current helper gets. It owns one `AsyncMonty` pool and a `kernel_id →
AsyncMontySession` table. The daemon talks to it over the existing
length-prefixed `ipc.py` framing, with new verbs: `open`, `feed`, `dump`,
`load`, `close`, and host-callback replies. Host callbacks stay id-correlated so
`gather` in a cell overlaps real work.

If the kernel host crashes, every live kernel is marked for restore (§3.8) and
the host is respawned on the next feed. A crashed **worker**
(`MontyCrashedError`) loses only that kernel.

Phase 0 observed no environment keys in the Monty worker on macOS 1.1.0,
but this is not a portable guarantee and Linux remains unverified. Keep the
scrubbed helper as the default defense-in-depth boundary.

### 3.4 Kernel identity and lifetime

- One kernel per `(session_id, agent_path)`, where `agent_path` is the root or a
  subagent's id. Subagents get their own kernel and never see the parent's.
- `kernel_id` is a random id recorded in `code.kernel_started`.
- Kernels outlive turns. They live in a `KernelRegistry` on the runtime (beside
  `_code_runner`'s current slot), not in the per-iteration tool manager.
- **Bounded.** At most `max_live_kernels` workers (default 4). A kernel idle for
  `idle_evict_s` (default 900) is checkpointed and its worker released; the
  least-recently-used one is evicted when the cap is hit. The next `code_run`
  restores it transparently (`code.kernel_restored`, `reason: "evicted"`).
  Eviction and restart therefore share one restore path.
- Closing or deleting the session closes its kernels and deletes their
  checkpoints.

### 3.5 Running a cell

1. Check the size, then type-check the cell against the stubs of what is
   attached (as `code` does today), before any host call.
2. Build `external_lookup` for **this** cell from the current iteration's
   callable catalog intersected with the kernel's attachments (§3.6).
3. `feed_run` with a per-cell deadline, `max_nested_calls` per cell, and the
   memory and recursion limits on the checkout.
4. Collect stdout and stderr, and the last-expression value.
5. Render the result (§3.7), emit `code.cell_finished`, then checkpoint (§3.8).

`In [n]` numbering is per kernel, survives restore, and restarts at 1 after
`code_reset`.

Nested calls work exactly as they do now: `ctx.invoke_tool`, `code.call` rows
with the cell's call id as `parent_call_id`, and permission prompts carrying
`parent_program`, which is now the cell source.

Errors come back as the cell's result with the diagnostic kinds code mode
already uses (`SyntaxError`, `TypeError`, `NameError`, `ToolError`,
`limit:memory|time|calls|output`, `timeout`, `cancelled`, `crashed`), plus
`KernelNotStarted` and `KernelLost`. An error never resets the kernel. Earlier
tool side effects are not rolled back; the manual says so.

### 3.6 Authority stays per turn

Attaching is a request, not a grant. Every cell rebuilds its callable set from
the **current** iteration's catalog (session disables, agent `restrict`, skill
allowlists, `research` profile), intersected with what is attached. A tool
disabled since `code_start` is missing from `external_lookup`, so calling it
gives `NameError: name 'x' is not callable now (disabled for this session)`.
The adapter maps the name to that message. The permission engine runs on every
nested call as it does today.

`expose` is the ceiling for implicit attachment through server selection or
query results (`read_only` by default). A tool explicitly named in `tools` may
be attached even if it is mutating; this selection is not a grant. Every call
still uses the current-turn callable catalog and passes all current permission,
authorization, and nested-call checks. Server- or query-derived attachment
remains subject to `expose`.

### 3.7 Cell output: what enters context

```
In [3]  ✓ 0.42 s · 3 calls · purpose: count open bugs per label
stdout:
  scanning 3 repos
Out [3]: {'bug': 12, 'ui': 4}            (dict, 2 keys)
calls:
  1 github_list_issues(repo='a') ✓ 18 KB
  …
```

- The value is rendered host-side: JSON-like values get a bounded pretty repr
  with type and size. A `MontyClassProxy` shows its class name and attributes.
  A value with no host form gets `repr()` evaluated in the kernel. `None` shows
  no `Out` line.
- Output is capped by `max_output_chars` (default 8000, split between stdout and
  `Out`). A clip is announced with full sizes and a hint (`try issues[:5] or
  len(issues)`).
- `whos()` is a kernel helper that returns a table of name, type and size/len,
  with no values. Phase 0 found top-level `locals()` returns the session
  namespace, while `globals()` and `dir()` do not work. Use the tested top-level
  `locals()` approach; bounded/safe extraction of arbitrary values remains an
  implementation concern.
- The nested-call appendix and image refs (`img-N`) work as today.

### 3.8 Durability: log first, snapshots as a signed cache

The **session log stays the source of truth**. Events (`nexus/events.py`,
`CODE_EVENTS` extended):

| Event | Fields (all redacted and bounded) |
| --- | --- |
| `code.kernel_started` | `kernel_id`, `agent_path`, `attached` {servers, tools, targets}, `skipped`, `monty_version` |
| `code.kernel_attached` | `kernel_id`, newly `attached`, `skipped` |
| `code.cell_started` | `kernel_id`, `call_id`, `n`, `source`, `purpose` |
| `code.call` | as today, with `parent_call_id` = the cell's call id |
| `code.cell_finished` | `kernel_id`, `call_id`, `n`, `status`, `diagnostic?`, `stdout_bytes`, `out_summary`, `calls`, `clipped` |
| `code.kernel_checkpointed` | `kernel_id`, `after_n`, `bytes`, `sha256` (no snapshot bytes) |
| `code.kernel_restored` | `kernel_id`, `from_n`, `reason` (`restart` \| `evicted` \| `host_crash`), `missing_cells` |
| `code.kernel_lost` | `kernel_id`, `reason` (`too_large` \| `invalid` \| `version` \| `missing`), `last_n` |
| `code.kernel_reset` | `kernel_id`, `stopped` |

**Checkpoints** are a separate SQLite table in `~/.nexus/nexus.db`
(`code_kernel_snapshots`: `session_id, kernel_id, after_n, monty_version,
bytes, mac, created_at`). Only the newest row per kernel is kept. They are
machine state, not part of the record or of JSONL export.

- A checkpoint is taken after every successful cell. If `dump()` is over
  `max_snapshot_bytes` (default 8 MiB), no checkpoint is written for that cell
  and the event says so. The previous checkpoint stays.
- Each snapshot is **HMAC-SHA256 signed** with a per-home key (created on first
  use, mode 0600, beside the other daemon secrets in `~/.nexus/`). This covers
  Monty's "snapshots are not authenticated" warning. The MAC binds `session_id`,
  `kernel_id`, `after_n`, `monty_version` and the bytes.
- **Restore** (first `code_run` or `code_start` after a restart, an eviction or
  a host crash): take the newest checkpoint; verify the MAC, the Monty version
  and the kernel id; `load_session` on a fresh checkout. Any cells that ran after
  it are listed in `missing_cells`. The cell result then **begins with a notice**:
  "Kernel restored from after In [7]. In [8]–[9] ran after that snapshot;
  variables they set are gone." With no usable checkpoint, the result is
  `KernelLost` with its reason, and the kernel is reset empty with its
  attachments kept. Cells are never re-run.
- **Replay equals live.** The reducer derives everything the UI shows from the
  events, not from snapshots.
- Snapshots may hold tool output (including anything secret a tool returned),
  just as tool results already in `nexus.db` do. They never hold provider
  credentials, which never enter the kernel.

### 3.9 Context, compaction and history

- Cell results are ordinary tool results and go through the existing result
  caps and context budgeting ([docs/context.md](../../docs/context.md)).
- **Kernel manifest on compaction.** When a session with a live kernel is
  compacted, the compaction input gets a bounded block: kernel id, attachments,
  cell count, and the `whos()` table (names, types and sizes only). The summary
  has to keep it, so the model knows its variables still exist and that
  `code_start` re-describes the kernel.
- The manual from `code_start` is ordinary history, so it is cached as part of
  the prefix and compacted like any other result.

### 3.10 Agents, profiles and guidance

- Registered for the `coding` profile only, as today. `research` refuses
  mutating nested calls, as today.
- Root roles that inherit the coding tools (e.g. `build`) get the four tools
  through the same iteration-manager selection the `code` tool uses now
  (`Runtime._make_tool_turn`).
- A subagent gets its own kernel when its definition allows the `code_*` tools.
  External workers (Claude Code, Antigravity) get none.
- Agent guidance ([docs/agents.md](../../docs/agents.md) defaults) gets one
  paragraph: prefer code mode for multi-step tool work; keep big intermediates in
  variables, not in context; look at slices.

### 3.11 TUI (Ratatui)

- Kernel lifecycle rows: `⧉ kernel started · github, linear (+14 tools) ·
  purpose`, plus attach, reset, restored and lost (lost in the error tone, with
  the reason).
- Cell rows in notebook form: an `In [n]` header with status, time, call count
  and purpose; the source (syntax-highlighted, collapsible); stdout; `Out [n]`;
  then the nested calls as child rows with full input and output (the existing
  `_code_sections` behavior). Clipping is announced in the row.
- The details sidebar gets a **Kernel** section for the active agent: id,
  attachments, cell count, last checkpoint (`after In [n]`, size) and live or
  evicted state.
- Old `code` rows in existing sessions keep their current rendering.
- Shared rendering goes in `ui_support/tool_details.py` (`_cell_sections`,
  `_kernel_sections`). The TUI-specific bits go in a new
  `ui/ratatui/codemode.py`, not in the bridge.

### 3.12 Config (`[code_mode]`)

Kept: `enabled`, `expose`, `pinned`, `context`, `max_tool_seconds`,
`max_nested_calls` (now per cell), `sub_model`, `max_sub_*`, `max_depth`.

Removed: `inline_budget_tokens` (replaced). `max_workers` becomes the kernel
host's concurrent-feed limit.

New, all bounded in `__post_init__`:

| Key | Default | Bound |
| --- | --- | --- |
| `manual_budget_tokens` | 6000 | 500–50000 |
| `max_output_chars` | 8000 | 1000–100000 |
| `max_live_kernels` | 4 | 1–32 |
| `idle_evict_s` | 900 | 60–86400 |
| `max_snapshot_bytes` | 8 MiB | 64 KiB–64 MiB |
| `max_cells` (per kernel) | 500 | 10–10000 |

`inline_budget_tokens` in an old config file triggers a one-line deprecation
notice and is otherwise ignored, so existing files don't fail to load.

### 3.13 Removing the `code` tool

- Delete the `code` registration, `store` / `load` / `store_delete` and
  `store.py`. Kernel variables replace the store. The removal is a
  `decisions.md` entry.
- Keep reducer handling of `code.started` / `code.call` / `code.store` /
  `code.finished` so old sessions replay and render unchanged (test-pinned).
- A permission rule or agent `tools` list naming `code` is treated as granting
  `code_run` (the permission key stays `code`). It is mentioned once in
  `nexus doctor`.

## 4. Phases

0. **Spike (no product code).** Answer every "not verified" item in §2 in a
   throwaway script under `artifacts/`, and record the findings in
   `docs/codemode-monty-compat.md`. Stop and revisit the plan if the worker env
   can't be scrubbed through the helper, or if dumps don't survive a fresh pool.
1. **Kernel host and registry.** `KernelHost` (helper verbs, crash respawn),
   `KernelRegistry` (identity, LRU, idle eviction, caps). Unit tests with a fake
   host.
2. **Tools.** `code_start` / `code_attach` / `code_run` / `code_reset`, the
   manual renderer (per-server stubs, budget, the "Not attached" list), per-cell
   authority, and output rendering. `code` is removed in the same phase.
3. **Durability.** New events, the snapshot table, HMAC, checkpoint and restore,
   lost handling, reducer folding, and replay-equals-live tests.
4. **Context.** The compaction kernel manifest and the guidance paragraph.
5. **TUI.** Notebook rows, the kernel sidebar section, and a PTY check.
6. **Docs pass and live trial.** One recorded live-model session using
   start → run → inspect → attach → restart → restore. Mark it "not verified"
   until it has been done.

## 5. Docs to update with the code

- `docs/tools.md`: rewrite "Code mode" around the four tools, the manual, the
  kernel and its limits.
- `docs/context.md`: the recursive-context section (`targets` moves to
  start/attach) and the compaction manifest.
- `docs/events-and-view.md`: the new `code.*` events and reducer folding.
- `docs/sessions.md`: the snapshot table, signing, and retention on session
  delete.
- `docs/config.md`: the `[code_mode]` keys.
- `docs/security.md`: the kernel host boundary and snapshot HMAC.
- `docs/agents.md`: per-agent kernels and the guidance.
- `docs/ratatui-parity.md`: notebook rows and the kernel section.
- `docs/decisions.md`: Monty REPL over IPython; start-handshake over an inline
  catalog; separate tools; restore-not-replay; the `code` removal.
- `docs/codemode-monty-compat.md`: the Phase 0 findings.
- `docs/module-map.md`: rows for `tools/codemode/kernel_host.py`, `registry.py`,
  `manual.py`, `cell.py`, `snapshots.py`, and `ui/ratatui/codemode.py`.

## 6. Tests

Next to their peers (`tests/test_codemode_*.py`):

- `test_codemode_kernel.py`: persistence across cells; the last-expression `Out`;
  an assignment-only cell has no `Out`; an error keeps state; reset clears and
  renumbers; stop then run gives `KernelNotStarted`; per-agent isolation.
- `test_codemode_manual.py`: per-server stubs; the budget clip is announced;
  unknown, disabled, excluded and `expose`-refused names are listed with
  reasons; re-describe lists state without changing it.
- `test_codemode_authority.py`: a tool disabled after `code_start` can't be
  called in the next cell, and the message says why; `research` refuses
  mutating calls; `code_*` tools can't be called from a cell.
- `test_codemode_snapshots.py`: checkpoint after success only; a size-capped
  checkpoint keeps the previous one; HMAC tamper, version mismatch and a wrong
  kernel id are each refused as `KernelLost`; restore lists `missing_cells`;
  deleting the session deletes its snapshots.
- `test_codemode_restore_e2e.py`: run cells, restart the runtime (and kill the
  kernel host), then `code_run` reads the restored variables and the result
  begins with the restore notice.
- `test_codemode_eviction.py`: LRU and idle eviction restore transparently.
- Reducer: replay equals live for every new event; old `code.*` sessions render
  unchanged (fixture under `tests/fixtures/`).
- Compaction: the manifest is present and bounded.
- `test_ratatui_pty_codemode.py`: extended for notebook rows and the restore
  notice.
- Existing `test_codemode_nested.py` and `test_codemode_injection.py` are ported
  to cells. Their guarantees don't change.

## 7. Decisions and open questions

Decided with the user (2026-10-24):

1. The backend is the Monty REPL session (`AsyncMontySession`), not IPython.
2. Separate tools: `code_start`, `code_attach`, `code_run`, `code_reset`.
3. After a restart, restore from a snapshot; never re-run cells.
4. Direct tools stay; code mode is optional but preferred.
5. The `code` tool is removed.
6. Explicitly named mutating tools may be attached; attachment remains subject
   to every current-turn permission and authorization check. `read_only` remains
   the default ceiling for implicit/server/query-derived attachment.

Open:
- **Resolved (default kept):** `whos()` and the manifest carry no value previews. Variable
  contents would enter context without the model asking. See §8.
- **Resolved (default kept):** a bare `code_start` attaches the built-ins plus `[code_mode]
  pinned`. See §8.

## 8. Implementation notes (2026-10-24)

Phases 0–5 are implemented; phase 6 (the live-model trial) is not verified. Deviations from
the text above:

- **Wording of restored cells (§3.8).** The header reads `↻ restored after In [8], 1 cell
  lost`, not "to rerun": cells are never re-run. The notice text stays as §3.8 gives it.
- **Verb-group labels (TUI).** Each kernel tool counts with its own noun:
  `Started N kernel(s)`, `Ran N cell(s)`, `Attached N kernel(s)`, `Reset N kernel(s)`
  (`ui/ratatui/verb_groups.py`). The label stays past tense while running (the native client shows
  `Working` separately).
- **Type check replaced by `argcheck.py`.** Monty's static checker is not used in the
  kernel (`type_check=False`). It infers each variable's type from its first binding, so a REPL
  rebinding `x = 1` then `x = 2` fails, and it forgets earlier imports. `tools/codemode/argcheck.py`
  parses each cell with `ast` and checks calls to attached tools against their JSON Schemas before
  any host call. It reports only certain mistakes. Details: [codemode-monty-compat.md](../../docs/codemode-monty-compat.md).
- **Subagents get no kernel yet (§3.10).** Known gap.
- **`code_run` exclusivity is runtime-wide.** `exclusive` is set while any mutating tool is
  attached to any kernel in the runtime, not only the calling agent's kernel. Known gap.
- **Idle eviction emits no event.** The sidebar cannot show "evicted". Known gap.
- **Unverified:** Linux execution, cross-version Monty snapshot compatibility, and the
  live-model trial (phase 6).

Docs: [tools.md](../../docs/tools.md), [context.md](../../docs/context.md),
[sessions.md](../../docs/sessions.md), [decisions.md](../../docs/decisions.md) (known gaps).
