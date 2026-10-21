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
`ask` → `mode`**. The synthetic `ContextQuery(<name>)` family's mode fallback is
always `ask` (even under `mode = allow`; `mode = deny` still denies).

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
- What the native prompt shows: `Allow <tool> <target>?`, how long it has waited, **What
  happens** (only what the request carries: its targets with their role and reason, else
  its preview), **Why it asked** (the rule that would be saved) and the decisions as
  neutral key chips ([ratatui-parity.md](ratatui-parity.md)). No tool supplies a richer
  preview (commit lists, diffs) today: not verified for every tool.

## Bundles and profiles (`bundles.py`)

| Bundle | Tools |
| --- | --- |
| `fs` | `read` `glob` `grep` `edit` `write` |
| `patch` | `apply_patch` |
| `shell` | `bash` |
| `task` | `subagent` `subagent_send` `subagent_stop` `subagent_list` `todowrite` `question` |
| `web` | `webfetch` `websearch` |
| `ext` | `skill` |
| `meta` | `ReloadExtensions` `ListExtensions` `WriteTool` |
| `mcp` | bridged MCP tools (names `mcp__<server>__<tool>`) |
| `legacy_fs` | `ls` `multiedit` (opt-in) |
| `legacy_shell` | `BashOutput` `KillShell` (opt-in) |
| `manager` | `sessions_find` `session_launch` `session_status` `session_wait` `session_result` `session_cancel` `session_send` (reserved, see below) |

Profiles: `coding` (default: fs, patch, shell, task, web, ext, mcp),
`coding_meta` (+ meta), `research` (read-only: drops every `mutates` tool,
including dynamic MCP ones), `chat` (none), `ops` (shell, mcp). An unknown
profile **fails closed** with an error. Permission `Bundle:shell` / `Bundle:fs`
also cover their legacy twins (historical rules).

### Manager bundle and profile (reserved)

`manager` is both a bundle and a profile (`MANAGER_PROFILE`, in `PROFILES` but
not in `profile_names()`), used only by the packaged manager session
([manager.md](manager.md)). Roles cannot name the bundle, the `manager` profile
or the `manager` context (the frontmatter grammar rejects them), and
`config.agent.profile = "manager"` would select a profile whose tools no ordinary
catalog contains.

The eight tools live in `manager_tools.py` and are bound to a `ManagerPort` by
`build_manager_catalog(port)`. The runtime builds that catalog **structurally**
(`Runtime._make_manager_tool_turn`): nothing from the manifest, MCP, skills,
extensions, hooks or the built-in catalog is merged in, and an injected
`tool_factory`, `ToolManager` or `PermissionEngine` never serves a manager
session. Bounds enforced in the tools: `session_launch` instruction ≤ 16 KiB
(UTF-8), required `origin_message_id` (a user message id; the host port checks
provenance), optional `agent` ≤ 64 and `title` ≤ 160 chars, **no model
override**; `session_wait` ≤ 20 ids and ≤ 600 s; `session_cancel` reason ≤ 256
chars; `session_send` message ≤ 16 KiB; `session_show` takes only an id and
`origin_message_id`. The host port enforces visibility, provenance, launch caps and
cancel-only-what-it-launched.

Permissions are a fresh `PermissionEngine` per manager turn: mode `deny`, every
manager catalog tool (reads plus `session_launch`, `session_cancel`,
`session_send` and `session_show`) allowed by name, so none of them ever asks, attended or not. Any
other tool name is denied, and the session's own unattended fallback is pinned
to deny for the turn. Session grants and `[permissions]` rules are never read.
Provenance, ownership and caps in the host port are the guard.

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
| `subagent` | no | spawns a bounded child; root sessions only, never present in a child's catalog, extension or code-mode context ([agents.md](agents.md)); params `prompt`, `subagent_type`, `tools`, `model` (a tier from the role's list, or a concrete model), `description`, `worktree`, `wait` (true = block for the report; with `agents.background` on, the default, and `wait` unset the call returns an acknowledgement at once and the child runs on in the background, [agents.md](agents.md#background-start-and-wait-agentsbackground-default-on)); the description lists each tier's models and effort, each role's tiers and says when to pick low, medium or high ([agents.md](agents.md#tiers-per-role)); a change to the tier models or efforts appends a short tail notice to history ([context.md](context.md#subagent-roster-notice-plan-06)) |
| `subagent_send` | no | continues a persistent subagent: params `agent_id`, `message` (≤ `agents.max_followup_chars`, over the limit is an error), `wait`; appends the message to the child's own conversation and runs another child turn, in the background unless `wait`; refused while it runs, once stopped, for external workers and for a worktree that cannot be reopened. Offered only with `agents.background` on (the default); root sessions only (as `subagent`); gate key is the **child's** `<role>:<tier>` and `Task(...)` rules apply ([agents.md](agents.md#persistent-children-subagent_send-subagent_stop-subagent_list)) |
| `subagent_stop` | no | cancels a running subagent and closes it for good (`agent.stopped`, slot freed; an idle child is also closed automatically, `by system`, when a new spawn needs its slot); params `agent_id`, `reason`; idempotent; same gating and key as `subagent_send` |
| `subagent_list` | no | one labelled row per subagent (id, role, model/tier, state, runs, last status, worktree, tokens, age, report pending), bounded to `agents.max_live` rows; read-only, allowed in every permission mode unless a `deny` rule names it |
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
- A run the daemon never finished (a crash or `kill -9`) is closed on the next
  open as `interrupted · not added to context`; its output is lost
  ([sessions.md](sessions.md) crash recovery). `shell.started` records the
  daemon `pid` for this.
- Not done: a daemon crash while a turn holds a deferred result loses it from
  model context (the timeline still shows it); no live progress rows.

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

### Code mode: the `code` tool

On by default (pinned `pydantic-monty==1.1.0` is a base dependency; disable with
`[code_mode] enabled = false`, see [config.md](config.md)). One tool,
`code`, runs a short Python program in the Monty sandbox so the model can fan out
and filter tool calls without putting intermediate results in context. The user
still sees every nested call with full input and output. Package: `nexus/tools/codemode/`.
Design history: `plans/planning/03-*.md`; Monty findings: [codemode-monty-compat.md](codemode-monty-compat.md).

**Input.** `program` (<= 16 KiB), `purpose` (<= 512 chars, shown to the user),
optional `timeout_s` (<= `max_tool_seconds`), optional `targets` (<= 50 session
names/ids, for context reads; see [context.md](context.md#recursive-context-code-mode)).
Permission key `code`; the tool is `exclusive` when mutating tools are exposed.

**Registration.** `code` is not in the builtin catalog; the runtime registers it
when it builds an iteration manager (`coding` profile, `code_mode.enabled`, Monty
available). A root agent definition's tool selection (`Runtime._make_tool_turn`) is
computed against that iteration manager, not the builtin catalog, so inheriting
roles such as `build` keep `code` in the turn's initial manager and schemas.

**Sandbox names are flat.** Each exposed tool is a bare async function
(`await read(path="README.md")`; MCP tools by their safe local name), plus
`search_tools`, `describe_tool`, `store`, `load`, `store_delete`, and with
`context = true` the `context_*` functions and `llm` and the `targets` variable.
Names that are unsafe or collide are skipped and listed in the description. Reason:
Monty 1.1.0 has no dotted-name lookup we could verify and flat names are shortest
for the model. `ToolError` is an alias of `RuntimeError` (Monty 1.1.0 has no class
inheritance), so `except ToolError` also catches any `RuntimeError`.

**Exposure** (`[code_mode] expose`):

| Value | Callable from a program |
| --- | --- |
| `read_only` (default) | Tools that do not mutate (`ToolSpec.mutates` false) |
| `all` | Every enabled tool, including mutating ones (each still permission-gated) |
| a list | Tool names and `mcp__server__*` globs; still limited to what the session has enabled |

Never callable, whatever the setting: `code`, `subagent`, `subagent_send`, `subagent_stop`, `subagent_list`, `question`, `todowrite`, `skill`,
`McpSearch`/`McpCall`, and the `meta` and `manager` bundles (`catalog.EXCLUDED`). They drive the
loop, the user or the agent tree and stay visible as top-level rows.

Direct tools stay available beside `code` (no "only" mode). A tool the session has
disabled is not callable. The `research` profile refuses mutating nested calls.

**Language.** Importable: `json`, `re`, `math`, `datetime`, `asyncio` (must be
imported), `collections`, `itertools`, `functools`, `dataclasses`, `typing`, `copy`,
`base64`, `random`, `time`. `os`, `sys`, `pathlib` are virtual and denied. Not
available: `string`, `textwrap`, `hashlib`, `urllib`, `enum`, `decimal`,
`statistics`, `operator`, `subprocess`; no `yield`, no `match`, no class inheritance.
Programs are type-checked against generated stubs (from each tool's JSON Schema:
`Literal` for enums, `X | None = None` for optional) before any host call.

**Catalog.** `render_catalog` lists per-namespace counts, then pinned stubs
(`[code_mode] pinned`) first, within `inline_budget_tokens` (2500); the rest are
reached with `search_tools` / `describe_tool`.

**Nested pipeline.** Every nested call goes through `ctx.invoke_tool` into
`_NestedPipeline` in `core/loop.py`; see [loop.md](loop.md#nested-tool-calls-from-code-mode).
It is the ordinary pipeline, not a bypass: validation, `PathGuard`, hooks,
permissions, timeouts, result caps. A denied path gets the same message a direct call
gets, and the program can catch it.

**Result format.** Per plan 03 section 3.10: status line, the program's output, a
bounded appendix listing every nested call, and image refs `img-N` (images attach by
reference only). Anything clipped is announced with sizes. Diagnostics name a kind:
`SyntaxError`, `TypeError`, `NameError`, `ToolError`, `limit:memory|time|calls|output`,
`timeout`, `cancelled`, `crashed`, or the inner exception class.

**Limits.** Source 16 KiB; `max_nested_calls` per program (default 64, counted per
`parent_call_id`); memory 64 MiB; recursion depth 200; no sleep; per-call deadline
`timeout_s`; runner admission is a semaphore of `max_workers` (default 2) and the
call reports busy after 5 s. Host callbacks are id-correlated and may overlap;
read-only nested calls overlap up to `tools.max_parallel`, mutating or `exclusive`
ones run alone. `store`: key <= 128 chars, value <= 64 KiB, total <= 512 KiB, JSON
only; writes commit **only when the program succeeds** and are replayed from
`code.store` events. Earlier tool side effects are **not** rolled back when a later
statement fails.

**Events.** `code.started`, `code.call`, `code.store`, `code.finished`
(`nexus/events.py:CODE_EVENTS`); fields follow plan 03 section 3.12 plus `skipped`,
`bytes`, `clipped`, `permission`. They fold in `view/reduce.py`; replay equals live.
`permission.requested`/`.resolved` for a nested call carry `parent_call_id` and
`parent_program`. Surfaces: `tool_details._code_sections` renders the labelled
sections ([ratatui-parity.md](ratatui-parity.md), [desktop.md](desktop.md)); the web
client has only the generic tool fallback.

**Not done / not verified.** Live-model trials (E1); Linux execution; Ratatui and
desktop visual review; structured results for bash/MCP depend on producers setting
metrics (not yet); per-row collapse on desktop.
