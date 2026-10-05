# Tools and permissions

`nexus/tools/` (L3) owns the tool contract, selection, validation, dispatch, the
permission engine, and the built-in tools. Tool names are **lowercase**
(`read`, `bash`, `subagent`); historical spellings (`Bash`, `Task`, …) are
translated one-way by `tools/names.py` and never widen anything.

## Files

| File | Owns |
| --- | --- |
| `spec.py` | `ToolSpec`, `ToolExecutionResult`, `ToolContext`, `RegisteredTool`, `PathTarget`, service views (`JobRegistryView`, `TodoStoreView`, …) |
| `manager.py` | `ToolManager`: ordered catalogue for one frozen profile, `prepare`, `dispatch`, result capping |
| `permissions.py` | rule grammar, `PermissionEngine`, `PathGuard`, `ApprovalBroker`, grants |
| `bundles.py` | bundles and profiles |
| `names.py` | legacy tool-name and permission-rule translation |
| `questions.py` | `QuestionBroker`: session-scoped agent questions |
| `loader.py` | hot-loads `.py` tool modules ([extensions.md](extensions.md)) |
| `builtin/` | the built-in tools and their private helpers |

## The contract

```python
ToolSpec(name, description, input_schema, bundle, mutates=False, concurrency="parallel",
         timeout_s=None, permission_key=None, max_result_tokens=25_000, version="1",
         path_mode=False, multi_path_targets=None, group="")
async def run(args, ctx: ToolContext) -> ToolExecutionResult
```

- Name grammar `[A-Za-z][A-Za-z0-9_]{0,63}`; `input_schema` is a JSON Schema
  object; everything must be JSON-encodable (no NUL bytes).
- Only `name`/`description`/`input_schema` are model-facing. `bundle`, `mutates`,
  `concurrency`, `timeout_s`, `permission_key` are harness-only.
- `permission_key` says what a rule matches: a resolved absolute path for
  `read`/`write`/`edit`, the command for `bash`, a `role:tier` key for
  `subagent`, a server-qualified name for MCP. Multi-path tools return
  `PathTarget`s (`role` = source/destination…), each evaluated.
- `ToolExecutionResult` → the IR `ToolResult` block; it can carry `display`,
  `metrics`, and a `context_note` (a one-line stand-in used when the result is
  evicted from context).
- `ToolContext` gives a tool the workspace, session/turn/call ids, config, a
  cancel token, a progress `emit`, `spawn_agent`, `invoke_tool` and narrow
  service views (jobs, todos, skills, extensions, questions, subagents, outbound
  HTTP). **It never exposes the `Runtime`**, which is what makes a hot-loaded tool
  reviewable.

## Manager (`manager.py`)

1. **Select** the catalogue from the frozen profile and config snapshot
   (immutable, ordered, deterministic schemas).
2. **Prepare** (`prepare`): unknown tool → error result listing valid names;
   strict JSON Schema validation; canonicalise permission keys through the
   `PathGuard`. Errors become ordered, model-visible results.
3. **Dispatch** (`dispatch`) only calls that have a decision: bounded parallelism
   (`tools.max_parallel` 8), `exclusive`/mutating tools run alone, per-tool
   timeouts (bash adds a 5s grace), cooperative cancel, exception → error result,
   result capping (`max_result_tokens`, ~4 chars/token; images ≈ 1600 tokens).
   The manager never prompts; it refuses to start a call with no decision.

## Permissions (`permissions.py`)

Grammar: `tool` (any arguments) · `tool(glob)` matched against the permission key ·
`Bundle:name` · wildcard names such as `mcp__server__*`. A `~` in a rule pattern
is rejected (keys are canonical paths).

Evaluation is first-match in a fixed order: **`deny` → session grants → `allow` →
`ask` → `mode`**.

- `deny` is absolute: no grant, path trick or model can override it.
- `PathGuard` canonicalises (`realpath`, symlinks followed, prospective writes
  resolve their existing parents) **before** any rule allow: `../` and symlink
  escapes fail closed. Hard boundaries: `write_roots`, `read_denyroots`, and the
  shared state DB (`nexus.db`) which no tool may touch. A Settings agent is
  further blocked from `credentials.json`, `sessions`, `cache`, `daemon*`, and
  trash.
- Approval is UI-agnostic: the gate emits `permission.requested` with a request
  id and awaits a future; any UI answers with `PermissionResolve`
  (`allow_once`, `allow_always`, `deny_once`, `deny_always`). First responder wins
  (`host/presence.py`). An `*_always` decision persists an **exact-action** rule
  (JSON-encoded key, so `*`, quotes and newlines stay literal), reconstructible
  from the log (`collect_grants`); a key too long or unencodable degrades to
  `*_once`. It can never broaden.
- No approver attached → `on_unattended`: `deny` (default), `allow`, or
  `fail_turn`. `PermissionEngine.require_confirmation_for(tools)` forces asks.
- Child agents inherit the parent's snapshot and grants and never widen them
  ([agents.md](agents.md)).

## Bundles and profiles (`bundles.py`)

| Bundle | Tools |
| --- | --- |
| `fs` | `read` `glob` `grep` `edit` `write` |
| `patch` | `apply_patch` |
| `shell` | `bash` |
| `task` | `subagent` `todowrite` `question` |
| `web` | `webfetch` `websearch` |
| `ext` | `skill` |
| `meta` | `ReloadExtensions` `ListExtensions` `WriteTool` |
| `mcp` | bridged MCP tools (names `mcp__<server>__<tool>`) |
| `legacy_fs` | `ls` `multiedit` (opt-in) |
| `legacy_shell` | `BashOutput` `KillShell` (opt-in) |

Profiles: `coding` (default: fs, patch, shell, task, web, ext, mcp),
`coding_meta` (+ meta), `research` (read-only: drops every `mutates` tool,
including dynamic MCP ones), `chat` (none), `ops` (shell, mcp). An unknown
profile **fails closed** with an error. Permission `Bundle:shell` / `Bundle:fs`
also cover their legacy twins (historical rules).

## Built-in tools

| Tool | Mutates | Notes |
| --- | --- | --- |
| `read` | no | text file or directory, `offset`/`limit` (default 2000 lines, hard 20,000); converts PDF/Office/ODF/RTF/EPUB to Markdown through the isolated AnyDoc worker (optional `documents` extra, ≤ 16 MiB); binary files are reported, not decoded |
| `glob` | no | deterministic, workspace-rooted; symlinks leaving the workspace are pruned |
| `grep` | no | literal or regex, runs in a killable worker (`tools.grep_timeout_s`), skips binaries and escaping symlinks |
| `edit` | yes | exact replacement; must match once unless `replace_all`/`occurrence`; returns a bounded diff artifact (≤ 240 lines) |
| `write` | yes | atomic, symlink-safe replace; parents created only with `create_parents` |
| `apply_patch` | yes | multi-file `*** Begin Patch` format (add/update/delete/move) with `@@` anchors, hunks located by content; ≤ 1,000,000 chars, 1,000 operations; `_patch_parse` → `_patch_stage` → `_patch_commit` (validate all, stage, guarded commit with rollback) |
| `bash` | yes | see below |
| `subagent` | no | spawns a bounded child ([agents.md](agents.md)); params `prompt`, `subagent_type`, `tools`, `model` (a tier from the role's list, or a concrete model), `description`, `worktree`; the description lists each role's tiers and says when to pick low, medium or high ([agents.md](agents.md#tiers-per-role)) |
| `todowrite` | no | agent-scoped in-memory task list (pending/in_progress/completed); full list each call; restored per session |
| `question` | no | asks the user one question (≤ 3 options, free text if none), waits up to 15 min; fails fast when no operator is attached |
| `webfetch` | no | public HTTP(S) → bounded Markdown wrapped in `<<< BEGIN UNTRUSTED WEB CONTENT >>>`; PDFs/binaries unsupported |
| `websearch` | no | fixed local SearXNG or allow-listed HTTPS instances; ≤ 10 results, pages ≤ 2; results wrapped as untrusted; links never fetched |
| `skill` | no | returns a skill body or bundled resource; activates the skill's tools for this turn |
| `ReloadExtensions` / `ListExtensions` / `WriteTool` | yes / no / yes | self-extension ([extensions.md](extensions.md)) |
| `ls`, `multiedit`, `BashOutput`, `KillShell` | | legacy, opt-in |

Web tools are filtered from the catalogue with an explanation when local search is
off and no HTTPS instance is configured, or fetching is disabled
(`BASE_TOOL_AVAILABILITY`).

### Shell jobs

`bash` (`action` run/status/wait/stop) uses `builtin/_jobs.py` (`JobRegistry`):
per-session job partitions (a tool can only reach its own session's jobs),
process-group lifecycle, output cap 1 MiB per job, SIGTERM then SIGKILL.
The runtime-owned registry inherits a copy of that runtime's captured environment.
A per-call `env` overlays only that job; it cannot mutate later jobs or another
runtime. An explicitly empty environment never inherits daemon variables.
Standalone registries capture the process environment at construction.
The inherited environment snapshot is never added to job results, progress
events, or reprs; a command can still explicitly print its own environment.

- Foreground runs block until exit, the yield window (`tools.bash_yield_s`,
  default 120s) or cancel. At the window a running command becomes a background
  job: result `status: running` + `job_id`; the model should `action=wait`
  once, not poll.
- `wait` defaults to `until="exit"` (optionally bounded by `wait_s` ≤
  `tools.bash_max_s`); `until="output"` returns on new output with a 30s cap.
  Each job keeps a read cursor so `status`/`wait` return only unseen output;
  explicit offsets override it.
- `timeout_s` is a hard kill limit capped at `tools.bash_max_s` (3600s), which
  also bounds every background job. Cancel while blocking kills the process
  group; cancel during `wait` stops waiting and leaves the job running.
- Throttled `tool.progress` events (last output line, 1 per 2s, ≤ 300 per call).
  Truncated output keeps head and tail. Shells are an allow-list
  (`/bin/sh`, `/bin/bash`, `/bin/zsh`); `workdir` must be inside the workspace.
- Not done: completion notifications for jobs still running at turn end.
- Tests: `tests/test_builtin_bash_*.py`; scenario `/mock bash-wait`.

## Adding or changing a tool

1. Write `nexus/tools/builtin/<name>.py` with `SPEC` and `async run`; register it
   in `builtin/__init__.py` and the right bundle in `bundles.py`.
2. Choose `mutates`/`concurrency` honestly (mutating ⇒ exclusive and gated) and a
   `permission_key` that canonicalises paths; bound every size and time.
3. Present it: `ui_support/tool_details.py` and `ui/web/js/tool-details.js` must
   show every parameter and output as labelled rows ([surfaces.md](surfaces.md)).
4. Tests next to peers: `tests/test_builtin_*.py`, `test_tool*.py`.

For an extension tool written by users, see [extending.md](extending.md).

Permission-key callback exceptions become `ToolSpecError` and a denied,
model-visible preparation error. Invalid subagent routing arguments must not
abort the whole turn or bypass the permission gate.

## Deferred MCP calls

The `mcp` bundle contains `McpSearch` and `McpCall` when at least one enabled
server uses search loading. Search accepts 1–8 queries (keyword or `select:name`)
and returns bounded, untrusted descriptions and schemas. Calls resolve their
real target before validation and permission evaluation. The target's name,
permission key, mutability, concurrency and server deadline govern execution.
`research` retains the proxy but refuses targets that change state.

`RegisteredTool.resolve` returns a frozen `ResolvedTarget` containing the spec,
runner and arguments. Preparation keeps the proxy call alongside this target;
permission batches use target calls so existing MCP wildcard rules and durable
grants keep their meaning. Filesystem targets cannot bypass path preparation.
