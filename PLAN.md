# Nexus 0.10 plan (branch nexus10)

Nexus rebuilt from scratch, starting at the provider wire: the exact JSON that
goes out and every event that comes back, token by token. It grows step by step
toward the two clients, the Ratatui TUI and NSH. Old Nexus (branch `nexus01`,
checkout `~/repos/nexus`) is reference and a parts bin. Nothing comes across
without a written reason.

Branch `nexus10` (an orphan branch, checked out as a worktree at
`~/repos/nexus10`). It is released as **0.10.0** once it replaces the old code
("10" is binary for 2).

## Principles

1. **Context is visible.** The user can see everything the agent sees: the
   request body, every stream event, every tool argument and result, labelled.
   Any clipping is announced.
2. **Raw and normalized, side by side.** Each part a provider returns keeps its
   exact wire item (`raw`) and the provider that made it (`origin`). A replay to
   the same provider is lossless (reasoning `encrypted_content`, item ids). Other
   providers use the normalized fields.
3. **Task tools vs. control.** Tools act on the workspace: read, bash, edit,
   write, and code to script them. Changing Nexus itself (which session, workspace or worktree, which
   model, forking, subagents) is a separate control plane in a `nexus`
   namespace. Those are commands with their own event type and permissions. The
   user can run them directly, and the agent gets them only when granted. A
   worktree is an optional property of a session, never a mandatory tool.
4. **One step at a time.** Each step ends with something you can run and see.
5. **Separate state.** The command is `nexus` (the old uv tool install was
   removed). State stays in `~/.nexus10` (`$NEXUS10_HOME`) so nothing is shared
   with old Nexus (`~/.nexus`), which still runs from its own checkout.
6. **uv only.** `uv sync`, `uv run`, `uv add`. Python is pinned in
   `.python-version` and dependencies are locked in `uv.lock`.
7. **One timeline.** The timeline the user sees and the context the agent sees
   stay as close as possible. Every item the model gets (system prompt, messages, tool
   results, notices, context insertions, handoffs) is shown to the user in
   order. A child launch appears in its parent’s context line; opening it shows
   the child’s own context line. Activity indicators (running, waiting, done)
   may update without becoming model context. Execution details are labelled
   separately from what the model received. Presenting context well is a core
   idea of Nexus.
8. **Append-only context.** Context already sent to a provider is never
   edited: it is cached, and a change invalidates the cache. A session's system
   prompt is fixed when the session starts. Later changes (a new directory, a
   switched setting) go forward as new items. Actions that will invalidate the
   cache anyway (switching model or agent) warn first.

## Layout

Each part of the repo is a self-contained project with its own toolchain. The
root holds only plans and docs.

```
docs/             how the code works; start at docs/README.md
INTERFACE_PLAN.md the TUI and NSH interface spec (both clients)
nexus-core/       Python (uv): everything that is not a UI. Import name `nexus`, command `nexus`
  pyproject.toml, uv.lock, .python-version
  nexus/
    llmhub/         providers normalized to one conversation + stream shape
      types.py      Message/parts (Text, Reasoning, ToolCall, ToolResult), Request, stream events
      sse.py        server-sent events parser
      responses.py  OpenAI Responses dialect (codex, grok) + ResponsesQuirks
      chat.py       OpenAI Chat Completions dialect (copilot, opencode) + ChatQuirks
      providers.py  the provider table: dialect, endpoint, models.dev id, quirks, default model
      catalog.py    models.dev catalog: download, 24 h cache, per-provider model list
      hub.py        joins sign-ins to the table; `provider/model` refs; default model
      auth/         each provider's sign-in; hands the dialects a header source
        store.py    ~/.nexus10/auth.json (0600, atomic, flock)
        device.py   RFC 8628 device flow (copilot, grok)
        codex.py, copilot.py, grok.py, opencode.py
    tools/          read, bash, edit (replacement or apply_patch), write, code; web_search/web_fetch planned
    kernels.py      Monty kernels for the `code` tool, owned by the daemon
    agents.py       agent files: frontmatter + prompt + mode; builtin, global, project
    builtin_agents/ orchestrate, build, quick, scout, plan (.md) + NOTICE
    loop.py         the agent loop (no storage; yields MessageAdded)
    codec.py        JSON for messages and events
    store.py        SQLite: projects, sessions (tree), messages, events
    bus.py          event bus
    runtime.py      projects, sessions, turns, approvals, interrupts, child sessions
    server.py       the HTTP API (OpenCode-style /api/*)
    inspector/      the session inspector page (served at /inspector)
    daemon.py       serve / start / stop / discovery
    client.py       HTTP client (used by the CLI)
    cli.py          local commands + daemon-client commands
  tests/
nexus-search/     Local SearXNG container configuration: Docker and Apple container
nexus-browser/    Default browser-use MCP configuration and setup (upstream server)
nexus-tui/        Ratatui terminal client (Rust). Not started
nexus-nsh/        NSH client: browser, desktop, iOS. Not started
```

Nexus Core runs as a daemon and exposes everything over HTTP (`docs/api.md`).
Every surface is a client of that API: the CLI today, and the TUI and NSH later.
No surface reads Nexus state files.

## Steps

### Step 0–2: wire, normalization, loop (done 2026-10-27)

- **Codex provider.** `POST {chatgpt.com/backend-api/codex}/responses` with
  `store: false`, `stream: true`, `include: ["reasoning.encrypted_content"]`,
  `prompt_cache_key` and `session_id` = conversation id, and
  `parallel_tool_calls: false`. The shape follows the Codex CLI
  (`codex-rs/codex-api/src/common.rs`).
- **Stream events.** `WireRequest` (credentials redacted), `WireResponse`,
  `WireEvent` (every SSE event as received), `TextDelta`, `ReasoningDelta`,
  `ToolCallStarted`, `ToolArgsDelta`, `PartDone`, `Usage`, `Done`.
- **Auth.** `nexus login codex` (browser) or `nexus login codex --device`. Only the
  refresh token and routing metadata are stored. Access tokens stay in memory.
- **Loop.** User message → stream → assistant parts → run tool calls → tool
  results → repeat until there are no calls (at most 50 steps). bash, edit and
  write ask for approval unless `--yes` is given; read never asks.
- **No sessions saved.** The conversation lives only in memory.
- **CLI.** `nexus chat|run [--model] [--effort] [--wire] [--wire-file F.jsonl] [--yes] [--workspace]`.
  `--wire` prints the request body and every SSE event. `/context` in chat
  prints the next request body.

Verified: a live run against `gpt-6.1-terra` made bash and edit calls over
three steps and a second turn, with items replayed in order and credentials
redacted in the wire file. `nexus login codex` works.

### Step 3: providers and models (done 2026-10-27)

Only four providers, all subscription sign-ins. No direct API-key vendors.

| id | sign-in | dialect | endpoint | models.dev |
| --- | --- | --- | --- | --- |
| `codex` | ChatGPT browser or device | Responses | chatgpt.com/backend-api/codex | `openai`, filtered to reasoning `gpt-*` |
| `grok` | SuperGrok device code | Responses | cli-chat-proxy.grok.com/v1 | `xai` |
| `copilot` | GitHub.com device code | Chat | api.githubcopilot.com | `github-copilot` |
| `opencode` | OpenCode Go API key | Chat | opencode.ai/zen/go/v1 | `opencode-go` |

- **Two dialects, quirks as data.** `responses.py` and `chat.py` each hold one
  builder and one parser. Per-provider differences are fields in
  `ResponsesQuirks` / `ChatQuirks` in the `providers.py` table, never branches.
- **Replay.** Codex gets its own output items back verbatim, including
  encrypted reasoning. grok gets rebuilt items, with no reasoning (store is off
  and there is no encrypted content). Chat hosts get their reasoning back under
  the field it arrived in (`reasoning_content` etc.). Reasoning from another
  provider is always dropped.
- **Models.** `nexus models [provider] [--all] [--refresh]` lists, for each
  connected provider, the models.dev models that can run an agent turn (text
  output, tool calls, not deprecated, not realtime/audio): context, output
  limit, price, reasoning efforts and release date. The catalog is cached for
  24 h in `~/.nexus10/cache/models.dev.json`, with a stale cache used when the
  download fails.
- **Model refs.** `--model provider/model`. The default is `$NEXUS_MODEL`, else
  codex's default if connected, else the first connected provider's default.

Verified live: copilot `claude-sonnet-5`, grok `grok-4.7` and opencode
`kimi-k3` each ran a bash tool call, a reply and a follow-up turn, with prompt
caching hits and Kimi reasoning replayed. These runs borrowed old Nexus's
stored sign-ins. **Not yet verified:** `nexus login copilot|grok|opencode`
themselves. Not every Codex-listed id is served by the Codex backend.

### Step 4: daemon, projects and sessions (done 2026-10-27)

- **Project.** A directory, unique by resolved path. **Session.** Belongs to a
  project and has a `directory` (the project path now, a worktree later), a
  model, a reasoning effort, a permission mode (`ask` or `allow`) and a title.
- **Storage.** Messages are saved as they are produced, in SQLite
  (`~/.nexus10/nexus.db`). Parts keep `raw`, so a resumed session replays
  losslessly. (Step 7 added the stored event log.)
- **Daemon.** `nexus serve` / `nexus daemon start|stop|status`, on 127.0.0.1:4747
  with a per-start bearer token in `daemon.json`. Clients auto-start it.
- **API.** An OpenCode-style `/api/*` subset (projects, sessions, prompt,
  interrupt, messages, context, permissions, providers, models) and one SSE
  stream, `/api/event`, carrying every event including the wire traffic.
  See `docs/api.md`.
- **Approvals.** Over the API: `permission.asked` → `POST …/permission/{id}`.
  Interrupting closes dangling tool calls with an error result.
- **CLI.** `run`, `chat`, `project` and `session` are now daemon clients.
  `login`, `models` and `status` stay local.

Verified: offline API tests with a scripted provider (permission flow,
interrupt, 409 while busy, 401 without token). Live against Codex: the CLI
auto-started the daemon, a tool turn ran, `-c` continued the same session with
its history, the effort setting reached the request, and curl drove the API and
SSE stream directly.

### Step 5: agents (done 2026-10-27)

An agent is a Markdown file. The frontmatter (`description`, `model`, `effort`,
`tools`, `steps`) configures the session, and the body is the system prompt.
Agents load from built-in, then `~/.nexus10/agents/`, then
`<project>/.agents/agents/`; a later one wins. A session runs one agent. The
user's model and effort override the agent's preference, and the session
reports which applied. `mode` (`primary`, `subagent`, `all`) says whether an
agent may run as a root session, a child session, or both, and it is enforced.

| Agent | Mode | Role |
| --- | --- | --- |
| `orchestrate` | primary | Delegates and checks; the only agent with the `session` tool |
| `build` | all | Writes and does the grunt work (OpenCode's GPT system prompt, adapted) |
| `quick` | subagent | Small tasks the parent should not carry in its context |
| `scout` | subagent | Finds what is relevant and returns a context brief (read, bash) |
| `plan` | subagent | Plans; may write Markdown only (prompt advice, not enforced) |

See `docs/agents.md`.

Verified: offline tests (parsing, precedence, problem reporting, tool
restriction, model/effort resolution, modes). Live on Codex: an agent limited to
read and bash refused to create a file and named its tools; `orchestrate`
delegated to `scout` and `build` (step 6).

### Step 6: session trees, child sessions instead of subagents (done 2026-10-27)

Every session records who launched it (`launched_by`: user or agent),
`parent_id`, and `parent_call_id` (the tool call that started it). An agent with
`control: [session]` can start a child session with another agent. The parent
either waits for the answer (`wait: true`) or carries on and is notified when
the child finishes (`wait: false`): the notice is added before its next model
call, or wakes it if it is idle. Children never get the `session` tool (one
level), and they run in the parent's directory. A child is a full session,
visible, resumable and inspectable. Interrupt and delete reach the whole
subtree. `/api/event?session=` includes descendants, and `GET …/tree` returns
the tree.

Verified: offline tests (blocking, non-blocking wake-up, interrupt cascade,
subtree filter, no tool for children, modes). Live on Codex: `orchestrate` sent
a `scout` child to read the workspace, had a `build` child add a function and
tests, checked the result itself with read-only commands, and reported (2 tests
passing). The CLI showed each child's events under its own header.

### Step 7: session inspector and durable event log (done 2026-10-27)

`nexus sessions inspector` opens a page served by the daemon. It has a sidebar
with the session trees and four levels per session: Conversation, Steps
(timing, usage, tools per model call), Requests (every request body as labelled
sections, and the next one), and Raw (every event). Every session event is now
stored in SQLite (`event` table, `GET /api/session/{id}/event`), so this works
for any past session. See `docs/inspector.md`.

Verified: offline tests (event paging, type filter, cascade delete, page
headers). Playwright screenshots in light and dark mode and at 390 px, with no
console errors and no horizontal overflow.

### Step 8: the `code` tool, Monty kernels (built and offline-tested 2026-10-27; review and commit pending)

A task tool, `code`, runs Python cells in a persistent **kernel**. Inside a cell,
the agent's task tools are async functions (`await bash(command="pytest -q")`,
`await read(path="…")`), so an agent can loop, fan out with `asyncio.gather`,
and filter results without putting each intermediate result in its context.
The sandbox is [Monty](https://github.com/pydantic/monty) (`pydantic-monty==1.1.0`),
as in old Nexus (`~/repos/nexus/nexus/tools/codemode/`,
`docs/codemode-monty-compat.md`). Old Nexus's kernel was 4 tools and about
4 000 lines; this is one tool and a small core first.

**Kernels live outside sessions.**
- **Start.** The first `code` call starts a kernel and returns its id (`krn_…`).
- **Default kernel.** A call without `kernel_id` uses the session tree's main
  kernel (the one its root uses), creating it if there is none. So a child
  session shares its parent's kernel by default, and can reach any live kernel
  by id. A variable set in one session can be read in another.
- **Ownership.** The daemon owns kernels (`nexus/kernels.py`), with a registry by
  id. Each kernel records who created it, its cell counter, and a lock, so it
  runs one cell at a time.
- **Lifetime.** A kernel lasts until the daemon stops or it is deleted
  (`DELETE /api/kernel/{id}`). It is not tied to a session's lifetime.

**Design**
- **Tool.** `code {code, kernel_id?, timeout_s?}`. A task tool, granted through
  an agent's `tools` like the others, and included when `tools` is omitted.
- **Tools inside the sandbox.** Inside a cell: the *calling* agent's task tools,
  minus `code` itself and every control tool. A `scout` cell gets only `read`
  and `bash`.
- **Nested calls.** Each one runs in the calling session's directory and goes
  through that session's approval (`permission.asked`, as a direct call would).
  Each is published as an event (`code.call`: kernel id, cell number, index,
  tool, arguments, output, error) with the parent `code` call id. A failing tool
  raises `RuntimeError` in the cell, so the cell can catch it.
- **Result.** Labelled text: `kernel krn_… · In [n] · 0.42 s · 3 calls`, then
  stdout, `Out [n]:` with the value's repr, and one line per nested call
  (tool, key arguments, ok/error, size). Errors show the exception's last line.
  Every clip is announced.
- **Limits.**
  - Monty enforces `max_feed_duration_secs` (sandbox compute only; host calls do
    not count, see the spike), `max_memory` 64 MiB, `max_recursion_depth` 200
    and `max_suspensions`, which bounds nested calls per cell.
  - The host enforces `timeout_s` on the whole cell, including tool calls
    (default 300 s).
  - The number of live kernels is capped.
- **API and UI.** `GET /api/kernel`, `GET /api/kernel/{id}` (cells, variables via
  a `whos()`-style helper, users), `DELETE /api/kernel/{id}`. The inspector shows
  nested calls under their `code` call, and kernels as their own list.
- **Isolation (decision to revisit).** Version 1 runs the `AsyncMonty` worker
  pool inside the daemon. Monty has no worker env/cwd controls, so workers
  inherit the daemon's environment; sandbox code cannot read it (no `os`, no
  files, see the spike), but a sandbox escape could. Old Nexus put the pool in a
  separate helper process with a scrubbed environment. Do the same before
  0.10.0.

**Spike (2026-10-27, Monty 1.1.0, macOS arm64, `artifacts/monty_spike.py`)**

| Check | Result |
| --- | --- |
| Globals persist across `feed_run` calls in one `AsyncMontySession` | yes (`x = 41`, later `x * 2` → 82) |
| Async host functions with keyword arguments (`external_lookup`) | yes |
| `asyncio.gather` over host functions | yes |
| A 3 s host call under a 2 s `max_feed_duration_secs` | completes: host time does not count, so the host needs its own cell timeout |
| `import os` + `os.listdir`, `open('/etc/hosts')` | `PermissionError` (no mounts, no `os` handler) |
| `import subprocess` | `ModuleNotFoundError` |
| A host function raising `RuntimeError` | catchable in the cell |
| Errors (`1/0`) | `MontyRuntimeError` whose last line is `ZeroDivisionError: …` |
| State after an error, and after a feed timeout (`while True`) | kept (`x` still 41) |
| `print` capture | `print_callback` gets `('stdout', 'hello\nworld\n')` |

Implemented in `nexus/kernels.py` and `tools/code.py`, with the kernel API and
inspector list/nested calls. Eight live kernels maximum; 64 nested calls per
cell; last 100 cells of metadata (omissions announced), with durable session
events retaining history. Variable previews track assigned names because Monty
has no `globals()`/`dir()`; the listing is approximate (200 names maximum).

**Lifecycle decision from implementation tests:** ordinary Python errors keep
state, but cancelling `feed_run` closes the checkout. Active-cell timeout,
interrupt, worker crash and resource-limit errors therefore discard the kernel
and announce state loss; the next default call starts a fresh one. Timing out
while queued leaves the active kernel intact. No checkpoint recovery in v1.

Verified offline with real Monty workers: persistence and cross-session sharing,
parallel host calls, nested approvals and durable events, caller directory and
tool restrictions (including saved host-function references), ordinary errors,
host timeout, cancellation, queued timeout, memory limit, live-kernel cap,
sandbox OS denial, clipping, API inspection/delete and session-independent
lifetime. Not verified: Linux, actual worker crash, cold-start latency, and
visual browser QA for the inspector additions.

Verified live on Codex (`gpt-6.1-terra`): orchestrate wrote `shared_probe` in
cell 1, a build child read and changed it in cell 2 without receiving its
contents in the prompt, and the parent asserted the changes in cell 3. All
three cells used the same kernel id. Orchestrate now explicitly grants `code`.
Repeat with `artifacts/shared_kernel_smoke.txt`; captured proof is in
`artifacts/shared_kernel_live_proof.json`.

**Not planned for version 1:**
- Kernel snapshots and restore after a daemon restart (old Nexus had signed
  checkpoints).
- Typed tool stubs and host-side argument checking.
- MCP tools inside the kernel.
- The `code_start`/`code_attach` handshake.

**Step 8 follow-ups (do before committing Step 8).** The Step 8 changes reached
the worktree from a background writer whose output was not seen during this
session (an interrupted earlier response). Review them as new code:
- `nexus/kernels.py`, `nexus/tools/code.py` and `tests/test_kernels.py`.
- The kernel routes in `server.py`.
- The `code` wiring in `runtime._agent`.
- The kernel list and nested `code.call` view in `inspector/index.html`.
- The doc changes in `docs/tools.md`, `docs/api.md`, `docs/inspector.md`,
  `docs/stream-events.md` and `docs/daemon.md`.

Then:
- Live-check a `code` cell on Codex: a gathered `read` and `bash`, and a child
  session reusing the parent's kernel.
- Re-run the inspector screenshots (`artifacts/`) for the kernel view.

### Step 9: programmatic tool descriptions and per-session tool switches (implemented and verified 2026-10-27; commit pending)

**Goal.** What the model is told about tools must match the tools it has. A
session can be started with tools switched off, and then nothing in its system
prompt or tool list mentions them. The `session` tool's guidance, its list of
child agents, comes from the agent files actually loaded, so a user can add
agents or change an agent's `mode` (for example, make `build` child-only) and
new sessions pick up the changes (see the session snapshot decision below).

**Built (uncommitted; 60 offline tests pass, lint clean)**
- **Tool guidance.** `Tool.guidance` (`tools/base.py`) holds one or two
  sentences per tool for the system prompt. read, bash, edit, write and code
  have it; the `session` tool builds its own (`runtime._session_tool`).
- **Rendering.** `agents.render_prompt()` replaces `{{tools}}` in an agent's
  body with a `# Tools` section listing only the session's tools, then adds the
  environment block (workspace, date). A body without `{{tools}}` gets the
  section appended. With no tools, it says so.
- **Built-in prompts.** All five are rewritten with `{{tools}}`, and none names a
  tool outside it. `orchestrate.md` no longer lists child agents; that list
  lives in the `session` tool's guidance.
- **Session guidance (current implementation).** Currently rebuilt every turn
  from the loaded child-capable agents. This is superseded by the session
  snapshot decision in Step 10: rebuild at new-session creation, not each turn.
- **Default agent.** A new root session uses `build` unless the agent files make
  it child-only. Then it uses the first agent allowed as a root
  (`runtime.create_session`).
- **Per-session tool switches.**
  - Sessions have `disabled_tools` (SQLite column, JSON list, migrated).
  - The API takes it on `POST /api/session` and `PATCH /api/session/{id}`.
    Unknown names are rejected with the known list.
  - Disabled tools are filtered out of the tool list, the prompt, and the tools
    callable inside `code` cells. `session` can be disabled too.
  - Session JSON now reports `tools` (what the next turn offers) and
    `disabled_tools`.
  - Child sessions inherit the parent's `disabled_tools`.
- **CLI.** `nexus run|chat --disable bash,write` (`''` turns all back on), `/tools`
  in chat, and switched-off tools shown in the session line.

**Completion checks (2026-10-27)**
- Added prompt rendering and built-in tool-agnostic body tests, API create/patch
  validation and round trips, persisted switches and pre-switch schema migration,
  empty-tool sessions, disabled direct and nested calls, child inheritance,
  dynamic child guidance/enum and root-agent fallback tests.
- Fixed user-created children to inherit `disabled_tools` when omitted, just as
  agent-created children do. An explicit list on user creation overrides it.
- Inspector session headers show available and disabled tools, including “none”.
- Updated agents, tools, API, daemon, inspector and development docs.
- Full offline suite: 60 tests passing; Ruff and `git diff --check` clean.
- Fresh daemon live check: Codex `gpt-6.1-terra` replied OK to
  `run --disable bash`; its API context omitted bash's schema and guidance
  (`ses_1a17bebc9d554301e81`). A temporary project's child-only `build`
  defaulted its root to orchestrate, retaining build in the session enum and
  generated guidance (`ses_1a17bec0f117f12e64b`).

**Remaining delivery work.** Step 8 and Step 9 still share an uncommitted
worktree. Complete Step 8's review and inspector screenshot follow-ups above,
then commit Step 8 and Step 9 as separate Conventional Commits and push
`nexus10`. No commits or push were made during Step 9 completion.

### Step 10: control plane

The rest of the `nexus` namespace: workspace, worktree, move. Commands
with their own events, exposed as API endpoints. Model and agent switching
already exist (`POST /api/session/{id}/model`, `/agent`).

**Frozen system prompt (do first).** Today `runtime._agent` re-renders the
system prompt every turn, with the workspace path and today's date, so a date
rollover or a directory change rewrites the cached prefix. Render it once when
the session starts (or when its draft is confirmed), store it with the
session, and replay it unchanged. Changes go forward as context items
(Principle 8). An agent switch is the one deliberate exception, and it warns.

**Session snapshots.** On creation of every session (root or child), check the
agent-definition files for additions, modifications and deletions and reload
changed definitions instead of trusting cached file contents. Resolve that
session's agent, granted tools and child-agent guidance once. Include only
available tools' schemas and descriptions in its rendered system prompt;
unavailable tools must not be described. Persist the resolved snapshot so later
turns and daemon restarts do not silently reload it. New children get a fresh
snapshot at their own creation, rather than inheriting stale agent definitions.
Existing sessions do not change because an agent file was edited. Explicit
mid-session agent/tool changes remain deliberate control actions, recorded in
context. Changes affect only subsequent calls. A running provider/tool call
keeps the configuration it started with. Snapshot the effective configuration
when dispatching each call; never mutate it underneath running work. Record
changes before the first subsequent call that uses them. Directory changes wait
for active tools to finish before applying. Agent changes are the deliberate
prompt-replacement exception and warn first; tool/MCP changes append guidance
and update subsequent request schemas without rewriting the frozen prompt.

**Instruction file.** Load `AGENTS.md` directly from the session's current
`directory`, for both roots and children. Do not search ancestor directories or
apply nested-directory discovery. Show its path and actual content in the
starting context. If the session directory changes, load the destination's
`AGENTS.md` and announce the new directory and instructions as forward context,
without rewriting the previously sent prompt. Define absence explicitly in the
context transition. Agent-definition files and this instruction file serve
different purposes; changing directory does not silently replace the selected
agent definition.

**`ask` tool (before workspace controls).** A task tool for questions to the user:
one or more questions, options (single or multi-select), free text and optional
previews. No timeout: wait until answered or the turn is interrupted, then
report cancellation in the result. Events: `question.asked` / `question.replied`.
Questions remain separate from approvals in Core; the interface combines them
as waiting for input. Implement this Core contract here before workspace controls and
before the interface's question component.

**Workspace edits need no approval.** Editing inside the session's directory
is allowed by default, with no permission-mode setting for that behavior.
Remove the existing approval requirement for in-directory `edit` and `write`
(including patches and nested calls). This does not remove path confinement or
the enforced read-before-edit invariant. Command execution outside that boundary
needs a separate explicit contract; unrestricted bash cannot guarantee it.

**Workspace controls: load on demand.** An always-available control tool,
`expose_workspace_tools`, lets the agent request the tools for working
in a different place. This is tool discovery/activation, not a user permission
request, and it does not move the session itself. Ordinary sessions start with
only this entry point; the detailed workspace controls are absent from their tool
schemas and system prompt until requested.

Its result appends the newly available tool definitions and guidance to the
context. Core also adds their executable schemas to the next provider request;
printing definitions alone does not make tools callable. Activation is persisted
per session, idempotent, visible in the timeline and survives daemon restart.
Do not rewrite the frozen system prompt. New children activate independently.

Expose exactly two tools after activation:
- **`change_session_directory {directory}`.** Move this session to an existing,
  validated directory. It does not create a worktree or move files.
- **`worktree {action: list|create|delete, ...}`.** List, create or delete Git
  worktrees. Creating a worktree returns its path; it does not change the
  session directory. The agent can then call `change_session_directory`.

These are control-plane operations with API endpoints and events; users can
also invoke them through client commands. No `worktrees: off|ask` setting and
no required `ask` question precede activation or moving. `ask` remains the
independent tool for actual questions.

**Directory transition.** A successful `change_session_directory` result names
the old and new absolute paths and branch when applicable, loads the destination
`AGENTS.md`, and explains that subsequent task tools resolve from the new path.
Earlier prompts and paths stay unchanged. A direct user/API move inserts the
same announcement before the next model call. Apply changes at a tool-safe
boundary so running calls cannot unexpectedly switch directory. While the
current directory differs from the frozen prompt's directory, bash results show
`cwd:` and path-taking tools report absolute paths. Clients update Files, git
status, headers and worktree grouping from the directory-change event.
Children created afterwards inherit the new directory; existing children keep
their own. File-read observations remain keyed by resolved file identity, so a
read at an old path never authorizes editing a different file at the new path.

**Worktree lifecycle.** Validate repository and target paths. Creation leaves
uncommitted changes in the original checkout. Deletion refuses dirty worktrees
and paths used by any session or active operation; do not force-remove them.
Leaving means changing the session directory first. Merge/integrate remains a
separate future operation. Worktree list/create/delete each have control events;
deletion and session deletion must not silently remove one another's state.

**Also in this step (from INTERFACE_PLAN.md):** export (Markdown transcript,
JSON of messages and events), archive/unarchive, and delete (already
cascades). No fork or revert: a session only moves forward.

### Step 11: event log follow-ups

Retention and size limits for the `event` table, resuming the live stream from
an event id, and showing a crashed turn's partial state.

**Reconnect contract.** Durable cursors survive daemon restarts (or explicitly
identify a changed log generation). Catch-up and live subscription share an
atomic boundary: no lost events between them. Replayed events have stable ids
and clients deduplicate them. Define retention expiry with an explicit
cursor-expired response that requires a fresh snapshot and cursor. Snapshots
include a watermark so subsequent events can be applied without races.
Filtered streams naturally skip unrelated global sequence numbers: a numeric
gap alone is not evidence of loss. Overflow, expired cursors or an explicit
continuity failure trigger resync. Reconnect restores pending questions,
approvals, queues, partial turns and session lists as well as visible text.

**Mutation contract.** Client request ids make retried prompts and other
mutations idempotent, with a defined deduplication lifetime and replayed
response. Concurrent clients get deterministic conflicts: approval/question
answers resolve once; stale queue edits and settings updates use revisions.
Document ordering of steer, queued prompts, setting changes and interrupts.
Verify disconnect during send, catch-up/live races, filtered streams, restart,
retention expiry and competing clients.

**Manual continuation (`/continue`).** A CLI chat/slash and client command backed
by a Core endpoint resumes model generation from the session's existing context.
Send that context as it stands to the selected LLM provider; do not append a
synthetic user message saying "continue", rewrite history or retry an old tool
call. Useful after a provider/network failure or an interrupted turn. Reject
while a turn is still running. The resumed model request and subsequent events
are recorded normally, with an explicit continuation link to the interrupted
turn/attempt so the UI can show what happened.

Completed tools and their saved results stay in context and are not re-executed
by continuation. Before offering continuation, interruption/restart recovery
must leave a provider-valid context: persist partial output where replayable,
close dangling tool calls with explicit interrupted/unknown-outcome results,
and announce any unreplayable partial output. Never claim a tool did not run
when its side effects are uncertain; do not automatically rerun such calls.
Recovery records are visible context added when interruption is reconciled;
`/continue` itself then sends the reconciled context unchanged. Test provider
failure before output, mid-text, mid-tool arguments, and after tool execution
but before a result is durably saved. Automatic retry policy is separate and
is not implied by this command; manual continuation is the initial recovery path.

### Step 11a: web tools, search service, skills and MCP

Planned, before the clients. Reference old Nexus deliberately; do not import its
configuration/state model or read `~/.nexus`. All new state uses `$NEXUS10_HOME`.

**Web tools.** Add `web_search` and `web_fetch` as task tools in Core, with the
same guidance, per-session switches, events, clipping and caller restrictions as
other task tools. They are callable from `code` when granted to the caller.
Reference `../nexus/nexus/tools/builtin/websearch.py`, `webfetch.py`,
`_html_markdown.py` and the shared `../nexus/nexus/net/` implementation.
- `web_search {query, limit?, page?}` uses the local SearXNG service; return
  bounded titles, URLs, snippets and available source/date metadata. Do not
  automatically fetch result links. Preserve the reference's deduplication,
  deadlines, cancellation and explicit untrusted-data labels.
- `web_fetch {url, timeout_s?, offset?, limit?}` fetches HTTP(S) text/HTML and
  returns bounded Markdown with source/final URL and line windows. Report
  unsupported binary content and HTTP/network errors clearly. Preserve URL,
  DNS and redirect validation, byte/time limits and announced clipping. The
  fixed local search endpoint is a separate exception from public URL fetching.
- Retain full outputs as bounded artifacts separately from model-visible text.
  Choose limits and document schema differences before porting code. Add offline
  fixture tests for search normalization, HTML conversion, redirects, blocked
  destinations, limits, errors, timeout and cancellation, plus a local service
  smoke check. Update `docs/tools.md` and provider/context docs with the code.

**`nexus-search/`.** Own the SearXNG settings and deployment documentation here,
referencing `../nexus/websearch/`. Include Docker Compose and Apple `container`
start/stop/log/health instructions, a private generated secret and a configurable
image version (pin a tested version before release). Use
`http://127.0.0.1:18765/search` by default, with JSON search enabled. No daemon
or client should silently start/install a container engine. Missing service is
an actionable tool error. Track config, never generated secrets or runtime data.

**Skills.** First-class Core support, not only an interface placeholder.
Reference `../nexus/nexus/skills/` and `tools/builtin/skill.py`.
- Discover built-in, `$NEXUS10_HOME/skills/` and session-directory
  `.agents/skills/` definitions; later roots override earlier ones. Parse
  `SKILL.md` frontmatter and report malformed, duplicate or shadowed entries.
- Check for file changes at every new root/child session and persist a stable
  snapshot. Initially render only enabled skill names/descriptions and source
  paths. A skill-loading task tool appends the selected instructions to context;
  label every loaded resource and every clip. Record activation durably.
- Skills do not silently grant tools or control capabilities. Enforce session
  restrictions for scripts/resources and nested calls. Resolve relative
  resources against the skill directory. Define how an explicit directory change
  refreshes project skills as forward context without rewriting the snapshot.
- Expose list/detail/source/diagnostics and session enable/disable through the
  API. Settings and session drafts consume these endpoints. Test precedence,
  reload at session creation, stable existing sessions and visible activation.

**MCP.** First-class Core client support, referencing `../nexus/nexus/mcp/`
(client, bridge, manager) and `../nexus/examples/mcp.json`.
- Configure built-in/default, global and project servers through Core; support
  local stdio and remote Streamable HTTP. Secrets come from environment/private
  configuration and are redacted from events, exports and connection diagnostics.
- Core owns connections, tool discovery, cancellation, timeouts, health and
  shutdown. Namespace tools by server to avoid collisions; bridge schemas and
  text/image results into the ordinary tool/context/event pipeline. Support
  resource listing/reading and explicit prompt loading as visible context.
- Only enabled, agent-allowed MCP tools enter a session's definitions/guidance.
  Record the actual schema snapshot used for each request. Changes/disconnects
  must be announced; never silently rewrite earlier context. A failed server
  must not break unrelated tools. Define session isolation for stateful servers,
  especially browser tabs/profiles, and preserve caller restrictions in `code`.
- API: server list/configuration, connection status, connect/disconnect/retry,
  tools/resources/prompts and session selection. Tests use a fixture MCP server
  for both transports, failures, tool collisions, cancellation, snapshots,
  secret redaction and restart recovery.

**`nexus-browser/`: default MCP.** Ship browser-use as the default configured
MCP server, enabled for new sessions subject to agent tool restrictions. This
folder contains upstream launch configuration and setup documentation, not a
custom browser automation implementation. Use the upstream local stdio server;
start lazily when needed and show an actionable setup error if unavailable.
Pin the tested browser-use release when implementing the integration. Document
browser installation and any credentials required by optional LLM-backed tools;
do not assume Nexus subscription sign-ins provide those API credentials.
Verify discovery, navigation, page state and screenshots with real browser-use.

### Step 12a: core for the interface

Core features the clients need before they can match
[INTERFACE_PLAN.md](INTERFACE_PLAN.md). Each is an API addition with events,
documented in `docs/api.md`.

- **Steer and queue.** While a turn runs, `prompt` with `mode: "steer"` adds the
  message before the next model call (after the current tool results), and
  `mode: "queue"` holds it for the next turn. Queued messages can be listed,
  edited and removed. Today a prompt while busy is a 409.
- **One-turn sessions.** A session kind that makes exactly one model call with
  no tools and ends, linked to its parent and hidden from default lists.
  Used for: the **title** (from the first user message, on a cheap model) and
  **`/btw`** (the parent's context up to its last completed tool call plus the
  user's question, so the cached prefix is reused; the answer never enters
  the parent's context).
- **Compaction as handoff.** No in-place compaction. At the configured context-use threshold, or
  on `/compact`, the session writes a handoff summary as its last turn and a
  new root session of the same agent starts from that summary, linked by
  `continued_from`. Both timelines preserve what their model saw. The automatic
  threshold defaults to **80% of the model context window**, editable as a
  numeric percentage in Settings (not a fixed set of presets). Define usable
  numeric bounds, token estimation and output/tool-result reserves. Trigger
  before the next request would exceed the threshold; preserve enough room to
  summarize. Define oversized-request and summary-failure recovery, handling of
  active children and queued messages, and settings carried to the successor.
- **Session drafts.** Create a session as a draft that returns its starting
  context as labelled sections (system prompt, instruction files such as
  `AGENTS.md`, tools, skills, MCP) with token counts, and accepts edits
  (switches, agent, model, prompt override) before the first message freezes
  it.
- **Archive, search.** `archived` on sessions; search over session titles and
  user message text only (an SQLite FTS5 index of exactly those fields).
- **Files and git.** List a project directory (lazy, one level, gitignore
  aware), read and write a file (confined to the project), git status and
  per-file diff of a session's directory.
- **Attachments and images.** Files and images on user messages, in both
  dialects (no images today). Store uploaded assets and full tool outputs as
  durable artifacts with stable ids and upload/download APIs. Enforce explicit
  per-artifact and aggregate size limits (choose defaults before implementation)
  and reject oversize uploads clearly. Record exactly which bytes or clipped
  representation reached the model; fetching full output never silently adds
  it to context. Define retention, export and deletion with owning sessions,
  including references shared by children and handoff successors.
- **Enforced read-before-edit.** Track each session's observed file revision
  and read range when file content actually enters its model context. Before
  editing, overwriting, deleting or moving an existing file, reject an absent
  or stale observation and require a fresh read. A child's read does not count
  for its parent; a read retained only inside a `code` kernel does not count
  as model-visible content. Define range coverage for partial/clipped reads.
  Use content revisions, not timestamps alone, and check again at commit time;
  serialize Nexus writers and detect external changes. Validate every affected
  file before a multi-file patch writes anything. New-file creation must refuse
  an unexpectedly existing destination. Apply this to direct and nested tools;
  editor/API writes carry their own observed revision and reject stale saves.
  **Enforcement prerequisite:** unrestricted `bash` can bypass tool checks by
  writing files directly; choose a filesystem enforcement mechanism or restrict
  write-capable command execution before claiming this invariant is guaranteed.
- **Version handshake.** `/api/info` reports an API version (major.minor).
  Clients halt on a different major. Together with Step 11's stream resume,
  clients can reconnect and resync.
- **Settings over the API.** Sign-in (browser and device flows), sign-out and
  provider status; defaults (model, effort, agent, tools). Workspace edits
  do not have a configurable approval mode.
- **Transcription (maybe).** Only if dictation uses a provider endpoint rather
  than the platform recognizer (decide with the dictation design).

### Release gate: 0.10.0

Before release, explicitly check off:
- Required scope: reviewed Steps 8–9, frozen context/control decisions, durable
  reconnect and restart recovery, interface Core contracts, and both clients'
  agreed first-release flows. Mark any deferred features explicitly.
- SQLite migrations tested from supported earlier schemas, with backup and
  failed-migration recovery; no access to old `~/.nexus` state.
- Restart/crash recovery preserves partial context, closes dangling tool calls,
  cancels pending waits visibly, and reports lost kernel state.
- Publish the supported OS/architecture and client capability matrix; run the
  core suite, real Monty workers and client smoke checks on each supported
  platform. Complete outstanding provider sign-in and inspector visual checks.
- Verify search/fetch, Docker and Apple container instructions, skills loading
  and the default browser-use MCP lifecycle.
- Verify context visibility, size limits, read-before-edit enforcement,
  multi-client/reconnect behavior and automatic handoff end to end.
- Keyboard-only navigation, focus restoration, streaming scroll anchoring and
  jump-to-latest verified in narrow and wide client layouts.

### Step 12: clients

Ratatui TUI (`nexus-tui/`) first, then NSH (`nexus-nsh/`), both on the HTTP
API and both built to one spec: [INTERFACE_PLAN.md](INTERFACE_PLAN.md) (layout,
sidebars, new-session view, timeline, tool and child-session rendering,
waiting-for-input, composer, palette, alerts, connection, settings). Both
show the wire view, the session tree and kernels.

## Changes from old Nexus (ledger)

| Old | New | Why |
| --- | --- | --- |
| Worktree is a tool, effectively mandatory | Worktree is a session property set from the control plane | Where work happens is not a task step |
| Task tools and Nexus-state tools mixed in one list | Separate task tools and `nexus` control commands | Different permissions, events and UI |
| Normalized IR only; provider details reconstructed | Each part keeps its `raw` wire item and `origin` | Lossless replay; nothing hidden |
| `~/.nexus` | `~/.nexus10` (until 0.10.0) | Old Nexus keeps working from its checkout |
| `nexus auth codex login` | `nexus login codex` | Sign-in is per provider, one verb |
| Provider ids `github-copilot`, `opencode-go`, `xai` | `copilot`, `opencode`, `grok` | Short names users type |
| Provider subclasses and host-detection branches | Two dialect modules + a quirks table | One place per difference |
| Many vendors (Anthropic, Gemini, NVIDIA, Ollama, agy, Claude agent…) | Only codex, grok, copilot, opencode | Subscription sign-ins only, for now |
| Custom host protocol (commands over a socket) | OpenCode-style HTTP `/api/*` + one SSE stream | Any language can be a client; curl works |
| Workspace is the unit | Project (a path) owns sessions; a session has its own `directory` | Room for worktrees per session |
| `auth/` beside the model layer | `llmhub/auth/` | Sign-in exists only to serve providers |
| Agent definitions mixed into config and code | One Markdown file per agent (frontmatter + prompt) | Readable, editable, per project |
| One general agent that also spawns helpers | `orchestrate` (delegates) vs `build` (does the work); children are `build`, `quick`, `scout`, `plan` | Clear roles; the orchestrator's context stays clean |
| Subagents run inside a tool call | Child sessions with `launched_by`, `parent_id`, `parent_call_id` | A child's work is a full, visible, resumable session |
| Four kernel tools (`code_start`, `code_attach`, `code_run`, `code_reset`), one kernel per agent | One `code` tool; kernels owned by the daemon, shared by id across sessions | Simpler to use; a session tree works in one kernel |
| Cancelling a cell could restore a signed checkpoint | Forced cancellation discards the kernel in v1 | Monty closes the checkout; checkpoint recovery is deferred |
| Live events only; messages in the log | Every session event stored (`event` table) | The inspector can show any past session down to the wire |
| Agent prompts name their tools in prose | `{{tools}}` placeholder filled from the session's actual tools (`Tool.guidance`) | A switched-off tool is never described to the model |
| Fixed list of child agents in the orchestrator's prompt | The `session` guidance is snapshotted from freshly checked agent files at each new session (planned, Step 10) | New sessions pick up agent changes without rewriting existing prompts |
| Worktree tool, effectively mandatory | On-demand workspace controls: change session directory and worktree list/create/delete (planned, Step 10) | Keep ordinary sessions small; activate workspace controls when needed |
| Compaction rewrites the session's history | A handoff to a new root session linked by `continued_from` (planned, Step 12a) | What the user sees stays what the model saw |
| System prompt rendered per turn | Rendered once at session start and replayed unchanged (planned, Step 10) | A stable cached prefix |
| Workspace edits require approval unless allowed | In-directory edits require no approval and have no setting (planned, Step 10); stale or unread files are still rejected | Editing authorization and read freshness are separate |
| Web tools named `websearch` / `webfetch`, deployment in `websearch/` | `web_search` / `web_fetch`, deployment in `nexus-search/` (planned, Step 11a) | Explicit names and self-contained service setup |
| Extension system and MCP integrations in old Core | Focused skills/MCP support and upstream browser-use default in `nexus-browser/` (planned, Step 11a) | Carry over needed capabilities deliberately |
| Separate TUI and NSH designs | One interface spec for both (INTERFACE_PLAN.md) | Same features, same names |

## Implementation sequence

Work one reviewed increment at a time:
1. Review and finish the existing Step 8–9 implementation, including pending
   kernel/inspector checks. Commit code only after review and validation.
2. Persist session snapshots and load session-directory `AGENTS.md` (Step 10).
3. Add `ask`, approval-free workspace edits and read freshness checks; resolve
   the unrestricted-bash enforcement boundary before claiming full enforcement.
4. Add on-demand workspace controls and directory/worktree operations.
5. Durable events, restart recovery, idempotent requests and `/continue`.
6. Bring artifact storage forward from Step 12a before web/MCP image results.
7. Web tools, skills, MCP and remaining interface Core endpoints.

Each increment updates matching docs and this plan, runs focused tests plus the
required suite/lint, and supplies a concrete diff and verification for review.
Do not start the next increment until the current review is complete.

## Known gaps

- Step 8 (`code`) and Step 9 (tool switches) are uncommitted. Step 8 arrived from
  a background writer and needs review before it is committed (see Step 8 follow-ups).
- No retries, no context limit or compaction (planned as handoff, Step 12a).
- The system prompt is re-rendered every turn (workspace, today's date), so a
  date rollover changes the cached prefix (fix planned, Step 10).
- The `event` table grows without limit (every token delta is a row); no retention yet.
- Sign-in is only the local `nexus login`, not yet an API endpoint.
- Nothing checks that a running daemon runs the current code: `nexus daemon stop` after changes.
- No cross-provider message conversion beyond text and tool calls; switching
  provider mid-session works (`/model`), but foreign reasoning is dropped.
- Copilot GPT models are sent over Chat Completions; whether some of them
  require Responses is not checked.
- No images in either dialect yet.
- Tool paths are confined to the workspace, but `bash` is not sandboxed, so
  "read-only" (`scout`) and "Markdown only" (`plan`) rest on their prompts.
- Child sessions launched with `wait: false` are tested offline only.
- `instructions` uses the Responses field. The current Codex CLI sends a
  developer message instead. Both work today; revisit if the backend changes.
