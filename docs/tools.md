# Tools and permissions

`nexus/tools/` (L3) owns the tool contract, selection, validation, dispatch, the
permission engine, and the built-in tools. Tool names are **lowercase**
(`read`, `shell`, `subagent`); historical spellings (`bash`, `Bash`, `Task`, …) are
translated one-way by `tools/names.py` and never widen anything. The shell tool
was called `bash`: `bash` and `Bash` remain exact aliases (never case-folded)
for calls, tool lists in role/skill files, permission rules, stored grants, hook
matchers, code-mode programs and `[code_mode] expose`; a call that arrives under
a legacy name also has `workdir` → `cwd` and `run_in_background` → `background`
translated (nothing else is). Wildcard rules and patterns such as `b*` or `Ba*`
keep matching the tool through its old spellings, so a stored wildcard deny stays
effective. Old session history is shown to the model as `shell` calls (the log is
untouched).

## Session execution roots

`ToolContext.workspace` is the session's selected checkout. `project_root`
identifies its shared configuration and state owner. Tool managers, file/edit
operations, grep and code-mode calls use a root-scoped `PathGuard`; configured
relative write scopes rebase to that root, absolute scopes are clipped, and
read-deny scopes retain both project and root locations. A worktree session's
path-aware tools cannot write to the main checkout or another worktree.

Shell cwd and `cd` stay inside the session root. Cwd records and shell logs are
stored under the project's state key, still partitioned by session id. Composer
`!` commands start at the session root and revalidate it before spawning. Local
Claude SDK workers receive the execution root per request; the shared provider
and captured credentials stay project-owned. Process admission applies to those
workers too.

These are path-aware tool boundaries and cwd checks. Shell commands retain the
existing command-permission policy; cwd bounds are not an OS filesystem sandbox.

Configured filesystem rules may match root-relative paths such as
`write(src/**)`. Canonical boundaries and configured denies are checked first;
absolute rules keep their absolute meaning. Exact file grants retain canonical
audit records, but replay uses the recorded root history to apply the same
relative file after that session changes roots. Shell/MCP grants and absolute
glob rules keep their original keys. Grants remain local to the session log;
this phase does not add a project-wide grant store or import other sessions'
approvals. Project settings and permission rules are shared independently.

Subagent worktrees are disabled for now ([agents.md](agents.md#worktrees)): every
child shares the session checkout and `worktree: true` is refused. When re-enabled,
`subagent.worktree` is independent of the session worktree mode. Omission chooses
isolation for a child whose selected tools can mutate files; explicit `false`
shares the session checkout and explicit `true` isolates it. Successful isolated
children return their saved diff automatically; conflict and failure results name
the retained branch. The root session's turn-save behavior remains unchanged.

## Session worktree tools

Every root session of a Git project starts in the main checkout and manages its
own worktrees; there are no worktree modes and no question before the first turn.
The Runtime adds four dynamic tools (none in a non-Git workspace; subagents never
get them):

| Tool | Does |
| --- | --- |
| `worktree_list {}` | Read-only. The main checkout plus every Nexus worktree of the project (name, branch, base, checkout present or removed, dirty, merged or unmerged with the commit count, the sessions in it with their titles and whether they are active, last activity, `← you are here`). Bounded to `worktrees.max_worktrees` rows with the clipping announced; extra Git worktrees Nexus did not create are listed separately as read-only info. Returns labelled fields, not JSON. |
| `worktree_create {reason, name?, base?}` | Creates `nexus/wt/<name>` (random word pair without `name`) and moves the session into it. From the main checkout the files this session wrote are carried over (proven by write receipts) and committed; from a worktree it branches from the updated base and leaves the old one as `worktree_move` does. Refused at the worktree limit, with the error naming merged worktrees that could be reused. |
| `worktree_move {to, reason}` | `to` is `"main"` or a worktree name from `worktree_list`, whoever created it and whoever else is in it (nothing is transferred, no other session moves). A removed checkout is recreated from its kept branch. Leaving a worktree needs its changes committed; unmerged commits are allowed and reported (`kept unmerged`); the branch is never deleted. |
| `worktree_commit {message, paths?}` | Only while the session is in a worktree. Unchanged: bounded to a 4,000-character message and 500 optional paths, each checked against the active root. |

`worktree_create` and `worktree_move` validate and return at once; the move is
**queued and applied at the next step boundary** of the running turn: after every
tool call of the model step finished and before the next model request
(`SessionWorktrees.step_boundary`, called from `core/loop.py`). The turn, its
step count and limits carry on. At the boundary Runtime closes the old root's part
of the turn (shell commits recorded, a harness `worktree.committed`, merge
detection), creates or recovers the target, emits `session.root_set` (with
`reason`, `request_id`, `action` and the `worktree_id`) and `worktree.created` /
`worktree.moved` / `worktree.promoted`, then rebuilds the per-root state — path
guard, tool set, shell cwd, cached root and the assembler — and opens the new
root's part with `worktree.turn_started`. Sibling tool calls of the same step still
run in the old directory (the tool descriptions say so). Compaction and queued
steers run at the top of the next iteration, so none lands between the request and
the move. If anything before `session.root_set` fails the root is unchanged and a
`worktree.move_failed` event plus a model-visible notice report it. If the turn
ends first, the queued move is dropped with `worktree.move_failed`. The
user-facing host actions (`move_session_worktree`, `promote_session`,
`detach_session_worktree`) keep their idle-only rule and apply immediately.

Host-facing Runtime API: `list_session_worktrees(session)` (the same rows the
model sees), `move_session_worktree(session, to, reason)` (idle; `"main"` or a
name), `promote_session(session, reason=, continuation=, name=, base=)` (idle
create; `name` only names the worktree and branch, never the session title) and
`detach_session_worktree(session)` (idle move to main).

**Permissions.** `worktree_list`, `worktree_create` and `worktree_move` never ask
(they only create or switch checkouts in Nexus-owned space and both can be
undone); the engine allows them by name in every mode, explicit deny rules still
win. Path permissions follow the root (`PathGuard.for_worktree`).

**Merging has no tool or command** and none is required (the agent guidance and
`/merge` say so: a `nexus` CLI without `merge` is not a blocker). Landing the
branch is plain Git in the shell: commit, squash, then
`git -C <main checkout> merge --ff-only <worktree branch>` (see
[decisions.md](decisions.md#agents-land-their-own-session-worktree-with-git-2026-10-23)).
The permission engine forces an ask for any shell command that runs a Git write
subcommand with `-C`, `--git-dir`, `--work-tree` or after a `cd`
(`_is_git_write_elsewhere` in `tools/permissions.py`), in every mode and despite
allow rules or grants (deny rules still win), with **one narrow exemption**: the
parsed landing (`is_worktree_landing`, [security.md](security.md)) never asks:
`git -C <main> merge --ff-only <branch>` and `git -C <main> checkout|switch
<base>`, including the documented `&&` chains (`checkout` then `merge`) and
`cd <main> && git …` spellings. Any other flag, order, redirect, pipe or
unknown path or branch keeps the forced ask.
Plain `git merge main` and read-only `git -C ... status` are not forced. Merge
detection (`worktree.merged`, deduped per landed head) runs at every step boundary
and at turn end.

## Files

| File | Owns |
| --- | --- |
| `spec.py` | `ToolSpec`, `ToolExecutionResult`, `ToolContext`, `RegisteredTool`, `PathTarget`, service views (`JobRegistryView`, …) |
| `manager.py` | `ToolManager`: ordered catalogue for one frozen profile, `prepare`, `dispatch`, result capping |
| `permissions.py` | rule grammar, `PermissionEngine`, `PathGuard`, `ApprovalBroker`, grants |
| `bundles.py` | bundles and profiles |
| `names.py` | legacy tool-name and permission-rule translation (`bash`/`Bash` → `shell`, legacy argument names, wildcard aliases) |
| `shell_syntax.py` | finds unquoted control syntax in a shell command for glob permission rules |
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
  `read`/`write`/`edit`, the command for `shell` (`status:<job>`, `wait:<job>`,
`stop:<job>`, `log:<job>` and `cd:<path>` for its other actions), a `role:tier` key for
  `subagent`, a server-qualified name for MCP. Multi-path tools return
  `PathTarget`s (`role` = source/destination…), each evaluated.
- `ToolExecutionResult` → the IR `ToolResult` block; it can carry `display`,
  `metrics`, and a `context_note` (a one-line stand-in used when the result is
  evicted from context).
- `ToolContext` gives a tool the workspace, session/turn/call ids, config, a
  cancel token, a progress `emit`, `spawn_agent`, `invoke_tool` and narrow
  service views (jobs, skills, extensions, questions, subagents, outbound
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
   timeouts (shell adds a 5s grace to `tools.shell_max_s`), cooperative cancel, exception → error result,
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
- **Compound shell commands.** A glob rule on `shell` (`shell(git diff*)`) used to
  match the whole command string, so it also allowed `git diff; curl x | sh` and
  a deny of `shell(rm -rf*)` missed `cd b && rm -rf .`. Now a glob **allow** rule
  only matches a command with no unquoted control syntax (`;` `&` `|` newline
  `` ` `` `$(` `<(` `>(` `(`, or an open quote; `2>&1` and `&>` are redirections,
  not control); otherwise the rule does not apply and the call asks (or follows
  the mode). Glob **deny** and **ask** rules (and hook matchers) are also tested
  against each segment split on that syntax, so a chained or substituted
  `rm -rf` is still caught. Exact rules, exact grants (what "always allow"
  saves), `Bundle:shell` and a bare `shell` rule are unchanged. This is a
  conservative lexer (`shell_syntax.py`), not a shell parser and not a sandbox.
- `PathGuard` canonicalises (`realpath`, symlinks followed, prospective writes
  resolve their existing parents) **before** any rule allow: `../` and symlink
  escapes fail closed. Hard boundaries: `write_roots`, `read_denyroots`, and the
  shared state DB (`nexus.db`) which no tool may touch. A Settings agent is
  further blocked from `credentials.json`, `sessions`, `cache`, `daemon*`, `shell`
  (job logs and session cwd records) and trash.
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
| `shell` | `shell` (was `bash`) |
| `task` | `subagent` `question` |
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

`subagent` always starts a new child; empty or placeholder optionals (`tools: []`, blank
`model`/`subagent_type`, a `description` with no letter or digit) are treated as omitted, and
an empty `tools` never means "no tools". Follow-ups are the separate `subagent_continue` tool
(`agent_id` + `prompt`, nothing else), in the same `task` bundle, so profiles, role tool lists
and permission rules for `subagent` cover it and children do not get it (root-only). It is
still one blocking call. Its permission key is `<recorded role>:<recorded tier>` read from the
child's stored record, not the default role, and `resume:unresolved` when the id cannot be
resolved (so `subagent(task:*)` does not authorize continuing an `advisor`). The runner still
refuses `tools`, `model`, `worktree` and a differing `subagent_type` on a resume. A Task call refused or failed before any child
exists draws a muted card (state `refused`/`failed` from `metrics.status`, model "not started",
the refusal reason from the result text, falling back to the short `error` label, without its `[model: …]` line) instead of "model pending"; "model pending"
stays for a call still running. See
[agents.md](agents.md#continuing-a-finished-subagent).

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
| `shell` | yes | `action` run/status/wait/stop/log/cd, see below |
| `subagent` | no | spawns a bounded child; root sessions only, never present in a child's catalog, extension or code-mode context ([agents.md](agents.md)); params `prompt`, `subagent_type`, `tools`, `model` (a tier from the role's list, or a concrete model), `description`, `resume` (`worktree` is hidden while subagent worktrees are disabled, [agents.md](agents.md#worktrees)); always blocks and returns the child's report as the result, so independent calls are issued together in one batch to run concurrently ([agents.md](agents.md#foreground-only)); the description lists each tier's models and effort, each role's tiers and says when to pick low, medium or high ([agents.md](agents.md#tiers-per-role)); a change to the tier models or efforts appends a short tail notice to history ([context.md](context.md#subagent-roster-notice-plan-06)) |
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

`shell` (`action` run/status/wait/stop/log/cd; the tool was `bash`) uses
`builtin/_jobs.py` (`JobRegistry`): per-session job partitions (a tool can only
reach its own session's jobs), process-group lifecycle, in-memory capture cap 1
MiB per stream, SIGTERM then SIGKILL. `tools/builtin/bash.py` is a one-release
import shim (its `run` accepts the old argument names).
The runtime-owned registry inherits a copy of that runtime's captured environment.
A per-call `env` overlays only that job; it cannot mutate later jobs or another
runtime. An explicitly empty environment never inherits daemon variables.
Standalone registries capture the process environment at construction.
The inherited environment snapshot is never added to job results, progress
events, or reprs; a command can still explicitly print its own environment.

- Foreground runs block until exit, the yield window (`tools.shell_yield_s`,
  default 120s) or cancel. At the window a running command becomes a background
  job: result `status: running` + `job_id`; the model should `action=wait`
  once, not poll.
- `wait` defaults to `until="exit"` (optionally bounded by `wait_s` ≤
  `tools.shell_max_s`); `until="output"` returns on new output with a 30s cap;
  `until="match"` returns when a new line matches `pattern` (≤ 120s, see
  Persistent jobs).
  Each job keeps a read cursor so `status`/`wait` return only unseen output;
  explicit `stdout_offset`/`stderr_offset` override it.
- `timeout_s` is a hard kill limit capped at `tools.shell_max_s` (3600s), which
  also bounds every finite background job (a `background` run without
  `timeout_s` is persistent and unbounded, see below). Cancel while blocking kills the process
  group; cancel during `wait` stops waiting and leaves the job running. The
  `tools.bash_timeout_s`, `bash_yield_s` and `bash_max_s` keys still parse as
  deprecated aliases, used only while the `shell_*` key is unset
  ([config.md](config.md)).
- Fields: `run` takes `command`, `cwd`, `timeout_s`, `background`, `env`, `shell`;
  a field that does not belong to the action is an error (the message names the
  renamed `workdir`/`run_in_background`). Shells are an allow-list
  (`/bin/sh`, `/bin/bash`, `/bin/zsh`); `cwd` must be inside the workspace.
- **Working directory.** `action=cd path=…` sets a persistent per-session cwd
  (recorded in `~/.nexus/projects/<key>/shell/<session>/cwd`, private file, so a
  reopened session or restarted daemon keeps it; not a session event yet). `run`
  starts in `cwd` if given (relative paths resolve against the session cwd;
  absolute ones must be inside the workspace), else the session cwd, else the
  workspace root. `..`/symlink escapes, `~`, missing paths and non-directories
  fail before anything runs, and the directory is re-checked right before spawn.
  A recorded cwd that was deleted (or left the workspace) makes the next run
  fail with an error naming it; `cd .` resets to the workspace root. A `cd`
  inside a command does not persist. Subagents and other sessions have their own
  record, and a child in its own worktree starts at that root. The `!` composer
  mode ignores the session cwd.
- **Full logs.** Every job tees stdout and stderr, in arrival order, into one
  log: `~/.nexus/projects/<key>/shell/<session>/<job_id>.log` (dirs `0700`, files
  `0600`, never in the workspace, environment values never written). Memory keeps
  the first 64 KiB and last 256 KiB of the combined stream. A result that fits
  the output limit renders as before (`stdout:` then `stderr:`); a result over
  it, or one whose per-stream capture was cut, is rendered from the combined
  stream instead (its last lines come last) and names the log and
  `shell action=log job_id=…`. `log` reads it back (`tail_lines`, `offset` +
  `limit` bytes, or `grep` literal text with line numbers), never more than 50 KiB
  per call; it works after the job was evicted from memory. Bounds, none silent:
  `tools.shell_log_max_bytes` per job (512 MiB, `0` = unlimited; unsaved bytes are
  counted in every result), `shell_log_total_bytes` per project (2 GiB) and
  `shell_log_retention_days` (7), both swept when a new job starts (running jobs'
  logs are never deleted). A write error stops logging for that job with a
  notice; it never kills the job.
- **Leftover processes.** When a command's shell exits while its process group
  still has members (`cmd &`), they are terminated (SIGTERM, grace, SIGKILL) and
  the result says so; use `background=true` instead (persistent jobs are exempt:
  their group lives until `stop`). `setsid` escapes are not tracked.
- Throttled `tool.progress` events (last output line, 1 per 2s, ≤ 300 per call).
  Output limit (`builtin/_output_limit.py`): a result shows at most 2,000
  lines or 50 KiB of output. Larger output keeps head (40%) and tail (60%) and
  a notice. Without a job log (the `!` composer mode) the full text is written
  to a private temp file (`$TMPDIR/nexus-output-<uid>/`, dir `0700`, file
  `0600`, newest 200 kept; `NEXUS_OUTPUT_DIR` overrides) whose path the notice
  names, so the agent can `read` or `grep` the rest.
- **Durable job events (P4, backend).** A job outlives its tool call, so its
  lifecycle is reported through an event sink on the registry
  (`JobRegistry(event_sink=…)`, `(session_id, type, data)`), not the tool's event
  stream. The runtime-owned registry's sink records turn-less session events
  through `SessionManager.emit_session_event`, which uses the owning session's
  **live** handle only (it never opens or unarchives a session):
  `shell.job_started` (after a successful spawn: `job_id`, `command` ≤ 500
  chars, `cwd`, `pid`, `background`, `started_at`), `shell.job_yielded` (the one
  foreground→background move: `reason` `yield_window` or `user`, `elapsed_ms`)
  and `shell.job_exited` (written once from finalization, after `job_started`:
  `status`, `exit_code`, `signal`, `duration_ms`, `background`, `log_bytes`,
  `log_lines`, `stderr_bytes`, `leftover_stopped`). Never output, environment
  values or log paths. A job finishing in the same tick it would be
  backgrounded stays a foreground result: `move_to_background` refuses, so its
  exit is reported once and no `job_yielded` follows. The reducer folds them into
  `ConversationView.jobs` (`JobView`, ≤ 64, absent from the wire until one
  exists; the first exit wins). Composer `!` runs are not announced (they have
  `shell.started`/`shell.completed`).
- **Host commands (P4, backend).** `SessionJobs`, `SessionJobLog`,
  `SessionJobStop`, `SessionJobBackground` ([host.md](host.md)) answer from the
  named session's registry partition (`host_support/session_jobs.py`); a client
  never reads the registry or a log itself. `SessionJobBackground` sets a request
  that the tool call blocking on the job honours at its next wake-up
  (`await_job(background_signal=True)`): it returns the usual background result
  saying the user moved it. It applies only while a tool call is awaiting the job
  in the foreground.
- **Lifetime guards (P4).** The daemon does not idle-exit while
  `Runtime.has_active_shell_jobs()`; the idle window restarts when the last job
  ends. `SessionManager(busy_check=…)` (the runtime injects
  `session_has_active_jobs`) makes a session with a running job un-evictable,
  un-archivable and un-deletable (`delete` raises `SessionBusy` even with
  `force`; stop the jobs first). `Runtime.aclose_session` first checks the
  manager's active-turn/viewer preconditions (`SessionBusy`, jobs untouched),
  then stops the session's jobs so their exits are recorded. A job stopped while
  its process is still starting kills that process and emits no events. Runtime shutdown closes the registry before the sessions.
- **Completion delivery (P5).** When a *background* job (started with
  `background`, or yielded) exits and nothing has returned its result, the exit
  reaches the model once, through the session's input queue:
  - **Atomic.** `shell.job_exited` and an `input.queued` notification are one
    `append_batch` (`Session.record_job_completion`), keyed
    `shell-job-<job_id>`: a repeat admission writes nothing and returns the same
    queued id. The registry stays below the session layer: it hands the exit and a
    bounded text to a `completion_sink`; `Runtime` routes it to the owning
    **live** session and a `job_completion_notifier` (installed by `HostFacade`)
    asks the supervisor to run it. Without a sink or live handle the exit is
    recorded plainly (`delivery: none`).
  - **When it runs.** The notification is a `steer`: an active turn consumes it at
    its next boundary (no extra turn); an idle session gets one turn; several
    exits before the turn starts are consumed together. Origin `shell_job` is
    *automated*: it skips `UserPromptSubmit` hooks, carries the `shell_job`
    message notice, and survives a Stop (like subagent reports).
  - **Claimed results.** A `wait` blocked on the job when it exits owns the
    completion (`delivery: tool_result`, nothing queued); a `wait` cancelled
    before it returned releases it and the notification is queued afterwards
    (the exit event is not repeated). A `status`/`wait`/`stop` that returned the
    terminal state retracts a notification still pending (`input.dropped`,
    "claimed by a shell result"). A user's Jobs-panel stop, session close,
    runtime shutdown and a child's end are *quiet*: nothing is queued.
  - **Stop and the cap.** A user Stop (`HostFacade.cancel` with `drop_queue`)
    pauses automatic wake-ups, recorded as `context.notice`
    `shell_wake_paused`; while paused completions queue as `queue` entries
    (`delivery: paused`) and start nothing. The next human message (typed turn,
    enqueue or admission) resumes, and a typed turn consumes pending
    notifications first. `tools.shell_max_auto_wakes` (10) consecutive
    notification-started turns pause the same way (`reason: max_auto_wakes`). The
    pause is in memory: a daemon restart forgets it (the queued notifications
    themselves are durable and start no turn on recovery).
  - **Content.** At most the last 80 lines / 8 KiB of the combined output, ANSI
    and control characters stripped, **every output line prefixed `| `** under a
    header stating it is untrusted program output granting no permission, plus
    job id, status, duration, log path and the `shell action=log` call. The
    command and cwd are deliberately left out (they are model- or user-written
    text that could forge structure). Never the full output.
  - **Code mode.** A `shell`/`bash` call with `background` (or legacy
    `run_in_background`) inside a code cell is refused with a `ToolError`
    and never dispatched: the cell ends with the call (the removed `code` tool had the same rule). The root role bodies
    (`build`, `orchestrator`) describe the notification and its untrusted output.
  - **Children.** Child and manager sessions take no completions. Jobs a child
    left running when its run ends are stopped (quietly) and listed in its report
    as `[stopped at child end: job_…]` (`SubagentRunner(stop_child_jobs=…)`).
- **Persistent jobs (P6).** `run` with `background=true` and **no** `timeout_s`
  starts a *persistent* job (`kind: persistent`, `timeout: none (runs until
  stopped)` in the result, `persistent: true` on `shell.job_started`, `mode`
  `persistent` in the host job row): no lifetime limit, so a dev server or a
  watcher survives the turn, a Stop and tool-call end. Details:
  - **Which calls.** With `timeout_s` a background job stays finite (capped by
    `tools.shell_max_s`). A legacy `bash` call using `run_in_background` without
    `timeout_s` is never made persistent: the tool manager (and the `bash.py`
    shim) mark the call out of band (`ToolContext.legacy_capped_background`), so
    `translate_legacy_shell_input` output is unchanged. A subagent's
    `background` without `timeout_s` is refused (nothing would own the job after
    it ends); code mode already refuses every `background` call.
  - **Limits.** `tools.shell_max_persistent_per_session` (4) running jobs per
    session and `tools.shell_max_persistent` (8) per runtime, checked in
    `JobRegistry.spawn` with no `await` between check and insert. Over the limit
    is an error listing **this session's** persistent jobs and how to `stop` one;
    the runtime-wide message never names another session's jobs.
  - **Duplicates.** A second persistent job with the same command and resolved
    cwd as a running one in the same session is an **error that names the
    running job**: the result starts `job_id: <running id>` / `status: running` /
    `kind: persistent`, and says to `wait`/`status`/`log` it or `stop` it first.
    It is an error rather than a success returning the first id because nothing
    was started (so a model must not believe it owns a new job), while the
    labelled `job_id:` line keeps it usable like every other job result.
    `env` and `shell` are not part of the identity.
  - **Readiness.** `wait until=match pattern=<regex ≤ 256 chars>` (`wait_s` ≤
    120) tests each *complete* new output line of the combined stream, ANSI/OSC
    escapes and control bytes removed and the line cut to 2,000 characters; a
    line split across chunks or polls is reassembled, and an unterminated last
    line is tested once the job has exited. "New" means output not yet returned
    to the model (the job's read cursor). The result gets a `match: <line>` header
    line; on timeout the job keeps running and the result says
    `match: none within Ns` and shows the output as `status` would. Matching
    runs in a thread with the `tools.grep_timeout_s` deadline, patterns that
    repeat a quantified group (`(a+)+`) are refused, and bytes the in-memory
    head/tail no longer hold are reported (`match_unscanned_bytes`) rather than
    skipped silently. A pathological pattern outside those shapes can still burn
    one worker thread until it finishes; it cannot block the event loop.
  - **`wait until=exit`** on a persistent job is capped at 600 s (default 600).
  - **Per-action deadline.** `ToolManager._timeout_for(spec, args)` is the
    action's own bound plus 5 s (`run` its `timeout_s`/`shell_max_s`, `wait` its
    `wait_s` or cap, `wait until=match` +15 s of scan allowance, `status`/`stop`/
    `log`/`cd` 60 s) instead of `shell_max_s` + 5 s for every action.
  - **Process group.** The job is its whole group: when the command's shell
    exits and the group still has members (a daemonised child), a persistent job
    stays `running` (polled every 0.25 s) and finishes when the group is empty;
    nothing is stopped as a "leftover". `stop`/shutdown signal the group
    (SIGTERM, grace, SIGKILL) even after the shell is gone. Once the group is
    seen empty nothing signals its group id again (it may be recycled by the OS).
    A group whose only members are zombies no init reaps would look alive; that
    needs a process that never reaps orphans and is not handled.
  - **Unexpected exit.** An exit nobody asked for is delivered once through the
    normal completion path (an idle session is woken once, subject to the P5
    wake cap and Stop pause); `stop` and shutdown are quiet.
  - **Orphan policy.** After `tools.shell_orphan_s` (3600; `0` disables) of
    *monotonic* time with persistent jobs running, no attached viewer, no running
    or queued turn and no live voice call, the daemon stops the persistent jobs
    and exits. The jobs are stopped first, while sessions are open, so each
    `shell.job_exited` carries `stopped: "no viewer for 1 h"` (the daemon log
    records `daemon.orphan_jobs_stopped` with the same reason); anyone arriving
    before the stop starts resets the clock. It applies even with
    `--idle-timeout 0`. Finite jobs still just keep the daemon from idling.
  - **Daemon status / stop.** Health carries `persistent_jobs_total` and a
    bounded list (≤ 16: session, job id, command ≤ 120 chars with secrets
    redacted, cwd, pid, start time); `nexus daemon status` prints them (`--json`
    includes them). `nexus daemon stop` goes through runtime shutdown, which
    terminates every group.
  - **Not done:** a Ratatui jobs panel beyond the `persistent` mode label; `label`.
- **Restart recovery (P7; implemented, not yet executed).** After a daemon is
  killed (`kill -9`, power loss) the next start stops the process groups it left
  behind, and a reopened session closes the jobs it lost. Design deviation from
  the plan: there is no `active.json` keyed by daemon PID (a PID is reused, and
  one file per daemon cannot be locked) — each `JobRegistry` has its own *owner*
  pair under `<home>/projects/<project-key>/shell-owners/` (`<home>` is the
  runtime's `home` override or `$NEXUS_HOME`, exactly like the session database):
  `owner_<16 hex>.lock`, held with `flock` for the registry's life (close-on-exec,
  so a job can never inherit it), and `owner_<16 hex>.json`, replaced atomically.
  The kernel releases the lock when the owner dies, so a record is **dead** only
  if its lock can be taken; a live owner is never swept. The directory is `0700`,
  files `0600`, opened with `O_NOFOLLOW`, regular files owned by the user only;
  records over 256 KiB, with a wrong version/owner/uid, or with an invalid job id,
  member or group are ignored (never signalled).
  - **Identity, not PIDs.** Per job the record keeps the group id and up to 8
    members as `(pid, start time from ps lstart, pgid, uid)`. A group is
    signalled only if a *fresh* listing shows a recorded member with exactly that
    identity (a recycled pid has another start time; a group id cannot be recycled
    while a member lives); SIGTERM, then — after another fresh listing re-verifies
    a member of the group as seen at TERM time — SIGKILL. `killpg(pgid, 0)` is
    never evidence. A persistent job whose shell exited stays verifiable through
    a surviving member: members are re-listed every 5 s and at leader exit.
  - **Sweep.** `Daemon.start` runs it under the daemon lock before binding the
    socket (`Runtime.recover_shell_orphans`, worker thread, 6 s budget, 8 s
    timeout; failures are logged as `daemon.shell_recovery_failed` and never stop
    the start; counts are logged as `daemon.shell_orphans_swept`). A record is
    deleted only once every group in it is settled; otherwise it is kept for the
    next start. It signals only groups of the current user and never its own.
  - **Session events.** A reopened session (under the exclusive session lock,
    in the same pass that closes interrupted turns) writes exactly one
    `shell.job_exited` — `status: interrupted`, `delivery: none`, `recovered:
    true` — for each announced job that has a `shell.job_started`, no exit, and
    that no live owner holds (`JobRegistry.job_may_be_live`, fail closed: no state
    directory, an unreadable live record, or a raising probe write nothing). It is
    a plain event: no queue admission, no notification, no turn, no wake.
    Idempotent, since the next open finds the exit. The event means "Nexus lost
    track", not "the process is dead"; the sweep is what stops the process.
  - **Graceful shutdown** (`aclose`) stops every group and deletes both files; a
    group that somehow survived keeps its record for the next sweep.
  - **Limits, not covered.** A kill between `fork` and the first listing
    (milliseconds) or before a freshly forked member is recorded (≤ 5 s) leaves
    that process untracked; a group whose recorded members all died before the
    sweep is treated as gone even if siblings survive; the sweep has a wall-clock
    budget, so with many stubborn groups the rest wait for the next start; a
    sub-agent session's jobs and `!` runs have no recovery event (the `!` run's
    own `shell.completed` recovery is unchanged; its group is swept like any
    other); platforms without `ps -o lstart`, NFS `flock` and a start time with
    one-second resolution are assumed away; and the daemon and `!` registries
    outside a `Runtime` (no state directory) track nothing.
- Not done: the TUI jobs panel and log viewer (Rust/Ratatui), and job events for
  sub-agent sessions (their handles are not in the main manager); deleting a
  session does not yet delete its log directory. Proposed, not implemented:
  [plans/SHELL_PLAN.md](../plans/done/SHELL_PLAN.md) (P4 UI).
- Tests: `tests/test_builtin_shell*.py`, `tests/test_builtin_bash_*.py`,
  `tests/test_shell_*.py` (`test_shell_job_events.py`,
  `test_shell_completion_delivery.py`, `test_shell_persistent.py`,
  `test_shell_restart_recovery.py`),
  `tests/test_host_session_jobs.py`; scenarios `/mock bash-wait`,
  `/mock shell-persistent`.

### Shell mode (`!` in the composer)

A composer draft starting with `!` runs the rest with `/bin/bash -c` in the
workspace (`host_support/user_shell.py`, host command `SessionShell`). It is
the user's own command, so no permission rule is consulted; it uses the
runtime's `JobRegistry` (same environment, capture cap and process-group
kill) and is bounded by `tools.shell_max_s`, 16,000 command characters and 4
concurrent runs per session. Stop (`SessionCancel`) kills running `!` commands.

- Durable events `shell.started` / `shell.completed` (`command`, `status`
  completed/failed/timed_out/cancelled, `exit_code`, `duration_ms`, `output`,
  `output_path`, `context`). The reducer draws each run as a `kind="shell"`
  turn: the `!command` user message and one `shell` tool row.
- The output uses the same limit as `shell` and is added to model context as a
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

### Code mode: the kernel tools

On by default (pinned `pydantic-monty==1.1.0` is a base dependency; disable with
`[code_mode] enabled = false`, see [config.md](config.md)). Code mode is a **kernel the
model opens, talks to and inspects**: four tools, `code_start`, `code_attach`, `code_run`
and `code_reset`, replace the removed one-shot `code` tool. The model fans out and filters
tool calls in persistent Python cells without putting intermediate results in context, and
the user still sees every nested call with full input and output. Package:
`nexus/tools/codemode/`. Design: `plans/planning/code-mode-kernel.md` (history:
`plans/planning/03-*.md`); Monty findings: [codemode-monty-compat.md](codemode-monty-compat.md).

| Tool | Input | Does |
| --- | --- | --- |
| `code_start` | `servers?` (<= 32 MCP server names), `tools?` (<= 256 names or `mcp__server__*` globs), `query?` (<= 256 chars), `targets?` (<= 50 session names/ids, [context.md](context.md#recursive-context-code-mode)), `purpose` (<= 512) | Opens this agent's kernel and returns the **manual** (below). On a running kernel it only re-describes it: nothing changes. With no arguments it attaches the built-ins plus `[code_mode] pinned`. |
| `code_attach` | the same | Adds tools or targets to the running kernel; the reply holds stubs for what is new only. |
| `code_run` | `cell` (<= 16 KiB), `purpose`, `timeout_s?` (<= `max_tool_seconds`) | Runs one cell. Permission key `code`; `exclusive` while mutating tools are attached anywhere in the runtime. |
| `code_reset` | `stop?` (default false), `purpose` | Clears all variables and restarts `In [n]` at 1, keeping attachments. With `stop` the kernel is freed and `code_run` needs `code_start` again. |

Descriptions are <= 600 characters each; `code_start` says code mode is preferred for
multi-step or fan-out tool work. `code_run` without a started kernel is
`KernelNotStarted: call code_start first`: the handshake is what puts the manual in context
(and `code_start` again re-describes a kernel after compaction summarized the manual away).
The tool list stays small and stable (better prompt caching); the manual is paid for only
when used and sits in context where the user can read it
([decisions.md](decisions.md#code-mode-the-code-tool-is-removed-start-handshake-over-an-inline-catalog)).

**Registration.** None of the four is in the builtin catalog; the runtime registers them
when it builds an iteration manager (`coding` profile, `code_mode.enabled`, Monty available;
`tools/codemode/tool.py:register`). A root agent definition's tool selection
(`Runtime._make_tool_turn`) is computed against that iteration manager, so inheriting roles
such as `build` keep them in the turn's initial manager and schemas. Naming any of the four
in `restrict` grants all four. **A permission rule or an agent `tools` list naming the old
`code` tool grants `code_run`** (`names.LEGACY_TOOL_NAMES`; the permission key stays `code`);
`nexus doctor` mentions the replacement once. Subagents get no kernel yet (as with the old
tool, children are built without it).

**Kernels** live in the runtime's `CodeKernels` (`kernels.py`), which outlives turns and
per-iteration managers: one `KernelHost` helper process (scrubbed environment, credentials
never enter the sandbox) with an async Monty pool, and a `KernelRegistry` keyed
`(session_id, agent_path)` (root or the subagent id). At most `max_live_kernels` workers;
a kernel idle for `idle_evict_s` is released with its state kept and restored on the next
call (`maintain()` runs on each tool call; there is no background task). Closing a session
closes its kernels and deletes their checkpoints. Not verified on Linux.

**Known gaps (kernel):** subagents get no kernel yet (plan §3.10 says they should);
`code_run`'s `exclusive` flag is runtime-wide (any attached mutating tool on any kernel);
idle eviction emits no event, so the sidebar cannot show "evicted"; Linux and cross-version
Monty snapshot compatibility are unverified; the live-model trial (plan phase 6) has not been
run ("not verified").

**Durability** (kernel plan §3.8, `snapshots.py`; storage and signing in [sessions.md](sessions.md#code-mode-kernel-checkpoints-code_kernel_snapshots)).
After every *successful* cell the kernel is dumped, signed and stored as its checkpoint
(`code.kernel_checkpointed`); a failed cell takes none, and a dump over `max_snapshot_bytes`
writes nothing, keeps the previous checkpoint and says so in the event. The first `code_run` or
`code_start` on a kernel that is not live goes through one restore path (`CodeKernels.prepare`):
`restart` (no worker after a daemon restart; the attachments, `In [n]` counter and `kernel_id` come
back from the session log), `host_crash` (the helper or worker died) or `evicted` (LRU or idle).
Restart and crash verify the HMAC, Monty version and kernel id, `load_session` into a fresh
checkout, and start the cell result with `Kernel restored from after In [7]. In [8]–[9] ran after
that snapshot; variables they set are gone.` (`missing_cells` is computed from the log; numbering
continues; cells are **never re-run**). An eviction restores the exact in-process snapshot the
registry took, so it needs no notice (`code.kernel_restored`, `reason: evicted`, no missing cells);
the checkpoint is the fallback if the process died after the eviction. With no usable checkpoint the
call returns `KernelLost (too_large|invalid|version|missing)` without running the cell, the kernel is
reset empty with its attachments kept, and `code.kernel_lost` records why. A timeout or cancel
discards the worker; with a checkpoint the next call restores it, without one the kernel resets
empty as before. Context targets approved before a restart are a grant of the old process and are
not read back from the log: `self` is re-approved, others are named in the notice and need
`code_attach(targets=...)` again. Without a state database (injected-manager test paths) there are no
checkpoints and a restart gives `KernelNotStarted`, as in Phase 2.

**The manual** (`manual.py`, the `code_start` reply) is plain labelled text in six sections:
1 how it works, 2 the language, 3 kernel helpers, 4 attached tools (typed stubs grouped under
`Built-in tools`, `Pinned tools`, `Server <name>`, `Named tools`, `Search matches`, bounded by
`manual_budget_tokens`: a stub that does not fit is listed by name with the clip announced
with counts; those tools stay attached and callable, `describe_tool(name)` shows the stub),
5 **Not attached** (every unknown server, session-disabled server, excluded, `expose`-refused,
unsafe or colliding name with its reason, reusing `Catalog.skipped`) and 6 state (re-describe
only: attachments, cell count, the `whos()` table).

**Sandbox names are flat.** Each attached tool is a bare async function with keyword arguments
(`await read(path="README.md")`; MCP tools by their safe local name). Kernel helpers:
`search_tools(query, limit)` and `describe_tool(name)` (any tool of this turn; they do not
attach), `whos()` (a text table of name, type and size, **never values**: Phase 0 found only
top-level `locals()` works, so the cell runner rewrites a bare `whos()` to `whos(locals())`;
call it at the top level of a cell), and `ToolError` (an alias of `RuntimeError`, so
`except ToolError` also catches any `RuntimeError`). With `[code_mode] context = true`:
`targets`, the `context_*` functions and `llm`, bound per cell from the targets approved at
start/attach. `code_start(targets=...)` asks `ContextQuery(<name>)` permission up front; a
denied or unavailable target is listed under "Not attached", never an error.

**Exposure and authority** (`[code_mode] expose`; kernel plan §3.6). Attaching is a request,
not a grant. `expose` is the **ceiling for implicit attachment** (server selection, `query`,
the defaults); an explicitly named tool in `tools` may be mutating. Every cell rebuilds its
callable set from the **current iteration's** catalog (session disables, agent `restrict`,
skill allowlists, the `research` profile) intersected with the attachments, and every nested
call goes through the full nested pipeline and permissions. A tool that is no longer
available gives `NameError: name 'x' is not callable now (disabled for this session or not
available this turn)`; one never attached says how to attach it.

| `expose` value | Implicitly attachable |
| --- | --- |
| `read_only` (default) | Tools that do not mutate (`ToolSpec.mutates` false) |
| `all` | Every enabled tool, including mutating ones (each still permission-gated) |
| a list | Tool names and `mcp__server__*` globs; still limited to what the session has enabled |

Never callable from a cell, whatever the setting: the four kernel tools, `code`, `subagent`,
`subagent_continue`, `question`, `skill`, `McpSearch`/`McpCall`, and the `meta` and `manager`
bundles (`catalog.EXCLUDED`). The `research` profile refuses mutating nested calls. Direct
tools stay available beside the kernel (no "only" mode).

**Language.** Importable: `json`, `re`, `math`, `datetime`, `asyncio` (must be imported),
`collections`, `itertools`, `functools`, `dataclasses`, `typing`, `copy`, `base64`, `random`,
`time`. `os`, `sys`, `pathlib` are virtual and denied. Not available: `string`, `textwrap`,
`hashlib`, `urllib`, `enum`, `decimal`, `statistics`, `operator`, `subprocess`; no `yield`, no
`match`, no class inheritance. **Static checking is host-side, not Monty's:** Monty's type
checker infers a variable's type from earlier cells (so `x = 1` then `x = 2` is rejected) and
forgets earlier imports, which a REPL cannot live with, so the kernel runs unchecked and
`argcheck.py` parses each cell with `ast` and checks every call of an attached tool against
its JSON Schema *before any host call*: positional arguments, unknown or missing keywords,
constants of the wrong type or outside an enum are a `TypeError` naming the lines and nothing
is called. Only what is certain is judged (variables, `**kwargs`, names the cell rebinds are
not). The tool's own input validation still runs on every call.

**Nested pipeline.** Every nested call goes through `ctx.invoke_tool` into
`_NestedPipeline` in `core/loop.py`; see [loop.md](loop.md#nested-tool-calls-from-code-mode).
It is the ordinary pipeline, not a bypass: validation, `PathGuard`, hooks, permissions,
timeouts, result caps. A denied path gets the same message a direct call gets, and the cell
can catch it. Permission prompts carry `parent_call_id` (the cell's call id) and
`parent_program` (the cell source).

**Cell output** (`cell.py`, what enters context):

```
In [3]  ✓ 0.42 s · 3 calls · purpose: count open bugs per label
stdout:
  scanning 3 repos
Out [3]: {'bug': 12, 'ui': 4}    (dict, 2 keys)
calls:
  1 github_list_issues(repo='a') ✓ 18 KB
```

`In [n]` numbers cells per kernel and is consumed even by a failed cell; it restarts at 1
after `code_reset` and after a kernel is lost. The last-expression value is rendered
host-side as a bounded Python repr with type and size (a multi-line string is shown verbatim
under its header; `None` shows no `Out`); the value reaches the host as JSON, so a tuple or
set shows as a list. Output is capped by `max_output_chars`, split between stdout and `Out`;
a clip is announced with the full size and a hint (`try big[:5] or len(big)`). Failures show
`In [n]  ✗ <Kind>`, the diagnostic and traceback, then the same call list. Diagnostic kinds:
`SyntaxError`, `TypeError`, `NameError`, `ToolError`, `limit:memory|time|calls|output`,
`timeout`, `cancelled`, `crashed`, `KernelLost`, `KernelBusy`, or the inner exception class.
`KernelNotStarted` is the answer to `code_run`/`code_attach` with no kernel. Image refs
`img-N` attach by reference only. A call list longer than 50 rows is cut with a count.

**Errors never reset a kernel.** A failed cell is not transactional: rebinding and mutations
made before the exception persist, and earlier tool side effects are not rolled back. The one
exception is a **timeout, cancel or lost worker**: the host cannot keep a half-run cell, so
the worker is discarded, the kernel is reset empty (`code.kernel_reset`, `reason: "lost"`,
attachments kept) and the result says so; a cancel is noticed on the next `code_run`.

**Limits.** Source 16 KiB; `max_nested_calls` **per cell** (default 64, counted per
`parent_call_id` by the loop; the host allows `2 * max_nested_calls + 16` suspensions up to
4096); `max_cells` per kernel; memory 64 MiB; recursion depth 200; no sleep; per-cell
deadline `timeout_s`; feeds admit `max_workers` at a time (default 2) and report busy after
5 s. Host callbacks are id-correlated and may overlap; read-only nested calls overlap up to
`tools.max_parallel`, mutating or `exclusive` ones run alone.

**Events** (`nexus/events.py:CODE_EVENTS`): `code.kernel_started` (`kernel_id`, `agent_path`,
`attached` {servers, tools, targets}, `skipped`, `monty_version`), `code.kernel_attached`,
`code.cell_started` (`n`, `source`, `purpose`, `callable`), `code.call` (running, then final;
`parent_call_id` is the cell's call id), `code.cell_finished` (`n`, `status`, `diagnostic`,
`stdout_bytes`, `out_summary`, `calls`, `clipped`, totals) and `code.kernel_reset`
(`stopped`, `reason`). Each lifecycle event carries its tool call's `call_id`, so it folds
into that tool row (`view/reduce.py:_on_code`); replay equals live (golden
`tests/fixtures/view/kernel_cells.*`). `code.started`, `code.store` and `code.finished` are
**no longer emitted** but still reduce, so old sessions replay and render unchanged
(`code_mode.*` golden). `permission.requested`/`.resolved` for a nested call carry
`parent_call_id` and `parent_program`. Surfaces: `tool_details._code_sections` renders the
labelled sections for `code` and the kernel tools ([ratatui-parity.md](ratatui-parity.md),
[desktop.md](desktop.md)); the web client has only the generic tool fallback.

**Removed with the `code` tool:** `store` / `load` / `store_delete` and `store.py` (kernel
variables replace the store) and `[code_mode] inline_budget_tokens`.

**Not done / not verified.** The compaction kernel manifest and the agent guidance paragraph (Phase 4); notebook
rows, a Kernel sidebar section and a PTY check in the Ratatui client (Phase 5); subagent
kernels; live-model trials; Linux execution; a nested-call cap above ~1000 per cell.

