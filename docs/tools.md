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

## Per-session selection

Before the first turn, the host's `ContextExtensionSelect` can switch individual
tools, skills, and MCP servers off and back on. Tool names match exactly (use
canonical names such as `read` and `subagent`). Disabled tools are removed from
the provider's schemas and dispatch catalogue, including dynamically added
subagent tools and MCP search wrappers. Agent allowlists remain authoritative:
tools excluded by the selected agent are hidden rather than offered as toggles.
Settings-disabled tools cannot be re-enabled by session selection.

The selection is persisted and replayed with the session. It locks while a turn
is active and permanently after the first `turn.started`; there is no additional
last-tool guard. Context inspection retains disabled selectable rows with
`enabled: false`, and `config_enabled: false` where Settings disabled a tool.
These rows retain the full description and input schema from the pinned tool
catalogue, so inspection can show the cost of re-enabling them. Enabled and
disabled rows include `source` (`built-in`, `extension`, or `mcp:<server>`),
`bundle`, and `read_only` (the inverse of the spec's `mutates`). Extension rows
include their source path as `origin`, workspace-relative where possible.
`timeout_s` is included only when declared; `permission` is omitted because
the current `ToolSpec` does not declare a permission category.

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
  Output limit (`builtin/_output_limit.py`): a result shows at most 2,000
  lines or 50 KiB of output. Larger output keeps head (40%) and tail (60%) and
  a notice; the full text is written to a private temp file
  (`$TMPDIR/nexus-output-<uid>/`, dir `0700`, file `0600`, newest 200 kept;
  `NEXUS_OUTPUT_DIR` overrides) whose path the notice names, so the agent can
  `read` or `grep` the rest. The 1 MiB per-stream capture cap still applies
  before that. Shells are an allow-list
  (`/bin/sh`, `/bin/bash`, `/bin/zsh`); `workdir` must be inside the workspace.
- Not done: completion notifications for jobs still running at turn end.
- Tests: `tests/test_builtin_bash_*.py`; scenario `/mock bash-wait`.

### Shell mode (`!` in the composer)

A composer draft starting with `!` runs the rest with `/bin/bash -c` in the
workspace (`host_support/user_shell.py`, host command `SessionShell`). It is
the user's own command, so no permission rule is consulted; it uses the
runtime's `JobRegistry` (same environment, capture cap and process-group
kill) and is bounded by `tools.bash_max_s`, 16,000 command characters and 4
concurrent runs per session. Stop (`SessionCancel`) kills running `!` commands.

- Durable events `shell.started` / `shell.completed` (`command`, `status`
  completed/failed/timed_out/cancelled, `exit_code`, `duration_ms`, `output`,
  `output_path`, `context`). The reducer draws each run as a `kind="shell"`
  turn: the `!command` user message and one `bash` tool row.
- The output uses the same limit as `bash` and is added to model context as a
  user message (`Session.add_context`). **It never starts a turn.** Idle: it
  is appended at once. During a turn it is held and appended at the next safe
  boundary (top of the next iteration, or after a final reply), never between
  a tool call and its result, and it never makes the loop take another step.
- A Stop that lands before a run's task starts still records a cancelled
  `shell.completed`, so no row is left looking like it is running.
- Not done: a daemon crash while a turn holds a deferred result loses it from
  model context (the timeline still shows it); a crash mid-run leaves a
  `shell.started` with no completion, drawn as running on replay (agent turns
  share this gap); no live progress rows.

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
