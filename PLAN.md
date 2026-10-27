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
    tools/          read, bash (background jobs too), edit (replacement or apply_patch), write, ask, code; web_search/web_fetch planned
    workspace.py    workspace control tools: expose on demand, move directory, worktree specs
    worktrees.py    Git worktree list/create/delete, through subprocess argument arrays
    kernels.py      Monty kernels for the `code` tool, owned by the daemon
    bash_jobs.py    background bash jobs, owned by the daemon
    snapshot.py     stored session configuration, tool schemas and AGENTS.md
    agents.py       agent files: frontmatter + prompt + mode; builtin, global, project
    builtin_agents/ orchestrate, build, quick, scout, plan (.md) + NOTICE
    loop.py         the agent loop (no storage; yields MessageAdded)
    codec.py        JSON for messages and events
    export.py       read-only Markdown/JSON exports from one SQLite snapshot
    store.py        SQLite: projects, sessions (tree), messages, turn lifecycle, events
    bus.py          event bus
    eventlog.py     durable event ids, cursors, replay-to-live stream
    recovery.py     startup reconciliation of turns a crash left unfinished
    runtime.py      projects, sessions, turns, approvals, interrupts, child sessions
    server.py       the HTTP API (OpenCode-style /api/*)
    inspector/      the session inspector page (served at /inspector)
    daemon.py       serve / start / stop / discovery
    client.py       HTTP client (used by the CLI)
    cli.py          local commands + daemon-client commands
  tests/
nexus-search/     Local SearXNG container configuration: Docker and Apple container
nexus-browser/    Default browser-use MCP configuration and setup (upstream server)
nexus-tui/        Ratatui terminal client (Rust): shell, timeline, commands (README)
nexus-nsh/        NSH client (TSX): browser + desktop, same interface (README)
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

### Step 8: the `code` tool, Monty kernels (reviewed and live-verified 2026-10-27; delivery reviewed)

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
A timeout or interrupt also cancels the cell's nested calls before the result
is reported: a running command is killed and a pending approval is withdrawn,
so nothing keeps changing the workspace after its cell is gone.

Verified offline with real Monty workers: persistence and cross-session sharing,
parallel host calls (including read and bash in one cell), nested approvals and
durable events, caller directory and tool restrictions (including saved
host-function references), ordinary errors, host timeout, cancellation, queued
timeout, memory limit, live-kernel cap, sandbox OS denial, clipping, API
inspection/delete, session-independent lifetime, cancelled nested calls and
commands, cancelled pending approvals, and shutdown with a running cell.
Not verified: Linux, an actual worker crash, cold-start latency.

Verified live on Codex (`gpt-6.1-terra`, 2026-10-27): orchestrate wrote
`shared_probe` in cell 1, a build child read and changed it in cell 2 without
receiving its contents in the prompt, and the parent asserted the changes in
cell 3. All three cells used the same kernel id. Orchestrate now explicitly
grants `code`. Repeat with `artifacts/shared_kernel_smoke.txt`; captured proof
is in `artifacts/shared_kernel_live_proof.json` (this run used an isolated
`$NEXUS10_HOME` and workspace, with a copy of the Codex sign-in).

**Not planned for version 1:**
- Kernel snapshots and restore after a daemon restart (old Nexus had signed
  checkpoints).
- Typed tool stubs and host-side argument checking.
- MCP tools inside the kernel.
- The `code_start`/`code_attach` handshake.

**Step 8 follow-ups: reviewed (2026-10-27).** The Step 8 changes had reached
the worktree from a background writer. They were read as new code, and these
defects were found and fixed:
- **Nested calls outlived their cell.** Monty abandons the in-flight host call
  when `feed_run` is cancelled. A `bash` command started by a timed-out cell
  kept running (it created a file after the cell was reported) and published a
  successful `code.call` afterwards. Cells now run nested calls as tracked
  tasks, cancel and await them on every exit path, kill a cancelled command's
  process group, and publish exactly one `code.call` per call, before
  `code.cell`. A pending nested approval is cancelled with the cell.
- **Shutdown could fail.** `Kernels.shutdown()` raised `ToolError` when a kernel
  was busy and left the pool open. It now stops the running cell, closes each
  worker exactly once and always closes the pool.
- **Inspector kernel view printed a literal `null`** (a null child passed to
  `replaceChildren`). It now renders only real nodes; the inspector also
  declares an inline favicon so visual checks have no console 404.
- **Docs structure:** the `code` paragraph in `docs/agents.md` sat under “Not
  yet”. It moved above that heading.

Regression tests added: ordinary errors keep state; timeout stops a nested
command, closes its call before the cell, and emits no late event; timeout
cancels a pending nested approval; parallel read and bash in one cell; direct
`bash` cancellation kills its process group; shutdown with a busy kernel; a
child's nested approval and events belong to the child; a saved tool reference
stops working when the session disables the tool.

**Inspector visual QA (2026-10-27).** Playwright (headless Chromium) against a
scripted daemon with one kernel: conversation nests three `code.call` records
(parallel read and bash, plus a caught read error); the kernel list opens a
detail view with variables and cells; light and dark mode; 390 px width. No
console errors and no document-level overflow. Screenshots:
`artifacts/inspector-kernels-{light,dark}.png`,
`artifacts/inspector-kernel-{light,dark}.png`,
`artifacts/inspector-kernels-narrow.png`, `artifacts/inspector-kernel-narrow.png`.

### Step 9: programmatic tool descriptions and per-session tool switches (reviewed and verified 2026-10-27; delivery reviewed)

**Goal.** What the model is told about tools must match the tools it has. A
session can be started with tools switched off, and then nothing in its system
prompt or tool list mentions them. The `session` tool's guidance, its list of
child agents, comes from the agent files actually loaded, so a user can add
agents or change an agent's `mode` (for example, make `build` child-only) and
new sessions pick up the changes (see the session snapshot decision below).

**Built and reviewed (77 offline tests pass, lint and `git diff --check` clean)**
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
- **Session guidance.** Step 9 rebuilt this every turn; Step 10's snapshot
  now freezes it from the loaded child-capable agents at session creation. New
  sessions pick up changes, existing ones keep their catalog.
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

**Review (2026-10-27).** The Step 9 code was read with Step 8 as one pending
diff; no functional Step 9 defect was found. The contracts were rechecked end
to end: unknown names rejected, switches persisted and migrated, empty-tool
prompts, `session` switched off, child inheritance and explicit overrides,
dynamic child guidance, and disabled tools absent from schemas, guidance and
nested code access. Added regression coverage for a saved tool reference that
stops working after its session disables the tool, a child's nested approval and
`code.call`/`code.cell` events belonging to the child, and parallel read + bash
in one cell. Moved the `code` paragraph in `docs/agents.md` out of “Not yet”.
Full suite: 74 tests passing (including Step 9a); lint and whitespace checks
clean.

**Delivery review.** Steps 8, 9 and 9a are delivered together as the reviewed
baseline: their runtime, tools, API, inspector and tests now share interleaved
changes. The reviewer independently reran the suite and lint, inspected the
saved live proof and representative screenshots, and fixed three additional
background-job defects: concurrent launches could exceed the live cap; an
explicit timeout skipped descendants after the shell exited; decoding each pipe
chunk separately corrupted split UTF-8 characters. Launch admission is now
serialized with shutdown coordination, the timeout covers the group through
output completion, and decoding is incremental. Three regression tests cover
these cases. Final suite: 77 passing; Ruff and whitespace checks clean. Saved
live/visual evidence was inspected, not regenerated during delivery review.

### Step 9a: bash without a default timeout, background bash jobs (requested and implemented 2026-10-27; delivery reviewed)

Requested while reviewing Steps 8–9. This is a distinct behavior change,
delivered in their shared reviewed baseline (see delivery review above).

- **No default time limit.** `bash {command, timeout?, background?}` waits until
  the command exits unless the caller passes `timeout` (seconds). The 120 s
  default and 600 s cap are gone. Cancelling the call (an interrupted turn or a
  code cell's `timeout_s`) sends SIGKILL to the process group.
- **Background mode.** `background: true` starts a daemon-owned job
  (`nexus/bash_jobs.py`, id `job_…`) in the caller's directory and returns at
  once. It survives the turn and the code cell. A finished job publishes
  `bash.finished` and adds a bounded output notice to the starting session's
  context — immediately (starting a turn) if idle, or before the next model call
  if busy — like a `wait: false` child.
- **Reading and stopping.** `bash_output {job_id, offset?, limit?}` reports
  status, exit code, duration and paged output. `bash_kill {job_id}` kills the
  group. Both are task tools granted with `bash` unless switched off
  individually; they only see their own session tree's jobs.
- **Limits.** 16 live jobs and 64 retained (running plus finished); the oldest
  finished job is evicted first. 1,000,000 characters of output per job, with
  dropped characters announced; 30,000 characters per read; 4,000-character
  tail in the notice. Jobs do not survive a daemon restart.
- **Lifecycle.** Interrupting a turn does not stop a background job. Deleting
  its session or project does, silently; daemon shutdown kills every job.
- **API and UI.** `GET /api/bash`, `GET /api/bash/{id}` and
  `DELETE /api/bash/{id}`; the inspector lists jobs beside kernels and opens
  their retained output. `bash.started`/`bash.finished` are durable session
  events.
- **Verification.** Offline with real processes: no default limit, optional
  timeout, cancellation kills the group, start/finish/timeout/kill, output
  ordering, tree scoping, session cancellation silence, finish notice in an
  idle session, `bash_output`, `bash_kill`, API list/detail/delete, session
  deletion killing its job, a code cell starting and reading a job, and the
  inspector job view in light, dark and at 390 px with no console errors or
  document overflow.
- **Live check (Codex `gpt-6.1-terra`, isolated home/workspace).** The model
  started `sleep 2; echo BACKGROUND_OK` with `background: true`, reported the
  job id, and the finish notice woke the session to report `BACKGROUND_OK`.
  Evidence: `artifacts/bash_job_live_proof.json`.

### Step 10: control plane

The rest of the `nexus` namespace: workspace, worktree, move. Commands
with their own events, exposed as API endpoints. Model and agent switching
already exist (`POST /api/session/{id}/model`, `/agent`).

**Session snapshot foundation (done 2026-10-27; delivered for review).** This
increment implements the frozen prompt, session snapshots and the
session-directory instruction file; it does not add `ask`, workspace controls,
skills, MCP, handoff or permission changes.

- **Storage.** A new `session_snapshot` table (`session_id` primary key,
  `data` JSON, `created`) is written in the same transaction as a new session.
  The JSON holds the resolved agent configuration (source, absolute path,
  fingerprint, body, mode, preferences, declared tools/control), the rendered
  agent prompt, the final system prompt, the enabled tool definitions
  (description, JSON schema, guidance), the child-agent catalog (name,
  description, mode, source, path, fingerprint), the `AGENTS.md` record (path,
  status, content, bytes, sha256, size limit), and the snapshot origin. No
  callables are stored; execution is rebound through the tool registry by name.
  An agent that lists a tool this build does not provide now fails session
  creation (it previously failed the first turn), because no usable snapshot can
  be resolved.
- **Creation and stability.** Root and child sessions load agent files fresh at
  their own creation. Turns and `/context`, the actual provider request, session
  JSON and the inspector all use the stored snapshot. Editing or deleting an
  agent file, changing a child-agent description, editing `AGENTS.md` or rolling
  over the date affects new sessions only (an unchosen model still follows the
  daemon default, as before). Agent switching rebuilds the snapshot deliberately;
  tool switches add/remove
  definitions for subsequent calls and append a visible `[Nexus notice: tool
  availability changed]` message without rewriting the frozen prompt.
- **Instruction file.** Exactly `<session.directory>/AGENTS.md` is read, with
  no ancestor or nested search. Its labelled `# Instructions file` section is
  part of `instructions`, separately from the agent prompt. Missing, unreadable
  and over-256-KiB files get a visible status and announced omission.
- **Migration.** Old sessions are initialized lazily on first use. The
  reconstructed snapshot says `origin: "reconstructed"`, carries a `# Snapshot
  note` saying it is not the exact historical prompt, emits `session.snapshot`,
  and is then stored and stable.
- **API and UI.** `GET /api/session/{id}/snapshot` returns the full stored
  snapshot; session JSON carries a bounded summary. The inspector shows the
  snapshot origin, agent source/fingerprint and instruction-file status, plus a
  snapshot card in Requests.
- **Verified offline** with scripted providers and temporary homes/workspaces:
  agent edits/deletions affect new sessions only; the same for `AGENTS.md`;
  missing, unreadable and oversized instruction files; restart stability; date
  rollover; independent root/child snapshots and deleted child agents; disabled
  tools absent from stored definitions, request schemas, prompt and nested code;
  forward guidance when clearing a switch; explicit model/tool updates applying
  within a turn, without changing running calls; old-schema reconstruction preserving
  messages and staying stable; agent switching rebuilding from current files;
  and `/context` matching the actual request. Delivery review added call-boundary refresh, ordered forward notices,
  bounded instruction-file reads, and restored control-tool exclusion from
  `code`. Regression tests cover same-turn changes, running-tool isolation and
  blocked nested session creation. Full suite: 92 tests; Ruff and whitespace
  checks clean. No live provider run was performed.

**Frozen system prompt (done in the foundation above).** `runtime._agent` used
to re-render the prompt every turn, with the workspace path and today's date, so
a date rollover rewrote the cached prefix. It is now rendered once at creation,
stored with the session, and replayed unchanged. Later deliberate changes go
forward as context items (Principle 8). An agent switch is the one deliberate
prompt-replacement exception.

**Session snapshots (done in the foundation above).** On
creation of every session (root or child), agent files are loaded fresh,
including additions, modifications and deletions. That session's agent, granted
tools and child-agent catalog are resolved once. Only available tools' schemas
and descriptions enter the prompt and request. The snapshot is stored, so later
turns and daemon restarts do not reload it. New children get a fresh snapshot at
their own creation. Existing sessions do not change because an agent file was
edited. Explicit mid-session agent/tool changes are deliberate control actions
recorded in context and apply to subsequent calls. A running call keeps the
configuration it started with. Agent switches are the prompt-replacement
exception; tool/MCP changes append guidance and update later request schemas
without rewriting the frozen prompt.

**Remaining in this step:** warn before an agent switch (the API currently
switches directly). Export and archive are done below. Workspace
controls, directory moves and worktree operations are done below.

**Workspace controls: on-demand exposure, directory moves and worktrees (done
2026-10-27; review pending).** This increment implements
`expose_workspace_tools`, `change_session_directory` and `worktree`; it does not
start export/archive, event recovery, web tools, skills or MCP.

- **On-demand exposure.** A new session's snapshot carries
  `workspace: {entry: true, activated: false}`, so `expose_workspace_tools` is in
  its request schemas and generated `# Tools` section. The two detailed tools are
  absent from both. Calling the entry point adds both definitions (description,
  JSON schema, guidance) to its model-visible result and to the stored snapshot,
  so the next request offers them. Activation is idempotent, survives turns and
  restarts, never rewrites the frozen prompt, and needs no approval or `ask`.
  Children have their own state. None of the three is callable inside `code`.
  Older snapshots are untouched; `POST /api/session/{id}/workspace-tools` (and
  `nexus workspace expose`) activates them explicitly.
- **Directory moves.** `change_session_directory {directory}` validates an
  existing directory, persists its resolved path, reads exactly the destination
  `AGENTS.md` and announces old/new paths, branch/repository, explicit
  missing/unreadable/too-large status, and that later calls resolve from the new
  path. Earlier messages, the frozen prompt, the original instruction file and
  project identity stay unchanged; the snapshot records the current instruction
  file and the last 20 moves. `session.directory_changed` carries from/to,
  branch, repository/worktree, instruction status, source and the frozen
  directory.
- **Transition timing.** A running call keeps its `ToolContext`; a move is queued
  and applied at the next model/tool dispatch boundary, or when the turn ends.
  A client request while busy returns 202 with
  `directory_change.status: "pending"` and the move is applied at that boundary;
  a second pending request is 409. Idle client moves apply immediately (200) and
  insert the same announcement as a user message. A tool-driven move's result is
  its announcement. Children created afterwards inherit the directory; existing
  children keep theirs.
- **Observations.** They remain keyed by resolved absolute path, so an old
  directory's read never authorizes a different file at the new path. While the
  live directory differs from the frozen prompt's, bash results start with
  `[cwd: …]` and path-taking tools report absolute paths.
- **Worktrees.** `worktree {action, path?, branch?, base?}` uses Git subprocess
  argument arrays (`nexus/worktrees.py`) and Git's own `worktree list
  --porcelain` metadata. List returns repository, main checkout, paths, heads,
  branches, flags and clean/dirty state. Create creates or reuses a validated
  worktree and returns its path without changing the session directory; existing
  non-worktree paths and missing parents are refused, and uncommitted changes in
  the original checkout stay there. Delete uses `git worktree remove` without
  `--force` and refuses the main checkout, bare/locked/missing or dirty
  worktrees, and paths used by any session, project or pending move; branches
  are never deleted. Create and delete share one lock and revalidate after
  waiting, so conflicting operations serialize. Events: `worktree.listed`/`created`/
  `deleted`.
- **API and CLI.** Endpoints: `POST …/workspace-tools`, `POST …/directory`,
  `GET`/`POST …/worktrees`; session JSON adds `workspace`,
  `starting_directory`, `directory_changed` and `directory_change`, and the
  snapshot summary adds workspace state, current instructions and move count.
  CLI: `nexus workspace expose|directory|list|create|delete --session …`. The
  inspector header and Requests card show the live/starting directory,
  activation and pending changes, and the new events update live.
- **Verified offline** with temporary homes, workspaces and real temporary Git
  repositories: hidden tools before activation and callable schemas on the next
  model call; idempotent activation and restart persistence; independent
  parent/child activation; exclusion from `code`; running-call isolation and
  rebinding later direct/nested calls; destination instructions loaded while the
  frozen prompt/history stay intact; missing/unreadable/oversized instructions;
  observations not transferring; new children inheriting and existing children
  retaining directories; worktree list/create/reuse/delete; dirty, main,
  in-use, invalid and occupied-path refusals; creation leaving the session
  directory unchanged; branch preservation; missing Git; API authentication and
  error responses; tool switches and agent switches after activation.
  Full suite after delivery review: 174 tests; Ruff and `git diff --check` clean. No live provider run
  was performed; the inspector changes were script-syntax-checked, not re-run
  visually.
- **Limitations.** A pending API move lives in memory (a daemon crash loses it
  before it is applied). Worktree deletion protects session/project directories,
  pending moves and daemon-owned background jobs, including subdirectories, but
  cannot detect an arbitrary external process using the path. There is no merge/integrate operation and no worktree permission
  setting. A directory move is not confined to the project; it accepts any
  existing directory the user can reach.

**Session-tree archiving (done 2026-10-27; delivery reviewed).** This increment
implements `archive`/`unarchive` for a whole root tree. It does not start export,
event recovery, web tools, skills or MCP.

- **Storage.** `session.archived` (`0`/`1`, not null) is added by migration; an
  existing database opens with every session unarchived. Every session of a root
  tree carries the same state. `Store.set_tree_archived` updates the root and all
  descendants in one SQLite transaction and reports whether anything changed.
  Archive state is distinct from `status` and changes no message, event,
  snapshot, file observation, directory, worktree, branch or background job.
- **Lifecycle.** `POST /api/session/{id}/archive` and `/unarchive` take the root
  id and are idempotent; a child id gets 400 naming the root. A root or
  descendant running a turn, waiting for an approval or waiting for an `ask`
  answer gets 409 and is not interrupted. A prompt on an archived session gets
  409 with the unarchive command; creating a child under it gets 409. Unarchive
  restores availability without starting a turn. Archived sessions remain fully
  readable through explicit session, tree, children, message, event and snapshot
  reads.
- **Lists and CLI.** `GET /api/session` adds `?archived=exclude` (default),
  `only` or `include`, applying to project, directory and parent lists. CLI:
  `nexus session archive|unarchive ID`, `session list --archived-only` and
  `--include-archived`; `--all` still means every project. `-c`/`--continue`
  skips archived sessions, and an explicit archived `--session` exits with an
  unarchive command.
- **Events and notifications.** One `session.archived`/`session.unarchived`
  event names the root, the tree ids and the new state; it is stored in the
  root's log. Repeating an unchanged operation publishes nothing. A background
  job that finishes while its session is archived still publishes
  `bash.finished` and saves the model-visible notice as a user message, without
  calling the provider; the notice is present after unarchive.
  The inspector loads archived sessions, shows an `archived` pill and header
  field separately from busy/idle, and refreshes on the transition events.
- **Verified offline** with isolated homes, scripted providers, temporary
  workspaces and real Git repositories: migration and restart persistence;
  whole-tree atomic archive/unarchive; idempotency and one transition event;
  busy-root and busy-descendant refusals; approval and question waits; child-only
  rejection; default/only/include filters; explicit reads; prompt and child
  creation refusals; unarchive without a turn; background completion notice
  without a provider call; continuation selection; preserved files, worktree,
  branch, snapshots and observations; API 401/400/404/409 responses. Full suite:
  190 tests after delivery review; Ruff and `git diff --check` clean. No live provider run was
  performed; the inspector changes were script-syntax-checked, not re-run
  visually.
- **Limitations.** The transition event is stored in the root's log, so a client
  subscribed only to a descendant's event stream does not receive it (global and
  root-tree subscribers do). Archive state has no timestamp, bulk project
  operation or automatic worktree cleanup: archiving deliberately never removes
  a worktree. Background jobs still finish and are not restarted by unarchive.

**Archive delivery review.** Independently reran suite and lint. Child creation
now reloads a cached parent before checking archive state, so an old Session
object cannot bypass the refusal. Added tests for this, arbitrary-depth busy
checks, whole-tree state changes and retained notices in archived descendants.
Final suite: 190 passing; Ruff and whitespace checks clean. No new live provider
or browser visual run was performed. Root-only transition delivery remains the
documented limitation for descendant-only subscribers.

**Session export (done 2026-10-27; delivery reviewed).** This increment implements
Markdown and JSON export; it does not start event recovery, web tools, skills or
MCP.

- **API.** `GET /api/session/{id}/export?format=markdown|json&scope=session|tree`.
  `format` is required; `scope` defaults to `session`. `tree` means the selected
  session and its descendants, never ancestors or siblings. Markdown is
  `text/markdown; charset=utf-8`; JSON is `application/json; charset=utf-8`.
  Both send `content-disposition: attachment` with
  `nexus-<id>-<scope>-<UTC>.<md|json>`, `x-nexus-export-format-version: 1` and
  an `x-nexus-export-boundary: event:<id>;seq:<seq>` header. Missing/unknown
  format or scope is 400, an unknown session 404, and the token is required.
  Archived sessions export as they are and are not unarchived.
- **Markdown.** A transcript: export metadata, tree outline, then one section
  per session with its fields, parent/child links, archive state, snapshot
  origin and instruction-file status, followed by the stored snapshot prompt and
  tool definitions. Messages keep stored order and ids;
  parts are labelled text, readable reasoning, tool call (arguments) and tool
  result. Encrypted reasoning is labelled present but unreadable and replayed.
  `[Nexus notice: …]` / `[Notification: …]` user messages are labelled context
  notices. Child conversations stay in their own sections. Existing clipping
  notices are reproduced; no extra clipping is applied. Fences are longer than
  any backtick run in the content.
- **JSON.** `nexus.session-export`, `format_version: 1`. The document carries
  scope, root, generation time, state, boundary and a `sessions` list. Each
  session has its stored metadata, tree links/depth, messages (the `/message`
  shape: ids, per-session seq, parts with `origin` and `raw`), every stored
  event in order, full stored snapshot (or `null`), snapshot origin/created,
  a snapshot notice, per-session counts and its own running state. Snapshot
  provenance is labelled: `created`, `agent-switch` (explicitly not the
  original historical prompt), `reconstructed` (the existing note), or missing
  (explicitly not reconstructed).
- **Boundary.** The export opens its own SQLite read-only connection and reads
  all records in one transaction; the watermark is the highest event row id,
  highest bus seq, newest selected `session.updated` and per-session counts.
  Writes after the boundary are excluded. A running session is labelled
  `durable_partial` with its running ids; export neither waits for nor
  interrupts it. Events are read in 1,000-row pages by `id > after` until a
  short page, not at the `/event` 5,000-event API cap; both formats stream.
- **Read-only.** Export never calls a provider, starts a turn, activates a
  tool, appends a notice or event, reconstructs a missing snapshot, reads a
  referenced workspace file or reads a credential. Wire transport headers
  are passed through the provider header redaction; message and tool
  text are preserved verbatim, with no text-pattern redaction.
- **CLI.** `nexus session export ID --format markdown|json [--scope session|tree]
  [--output FILE] [--force]`. Without `--output` (or with `-`) it writes stdout.
  An existing file is refused unless `--force` is given. The download goes to a
  temporary `.part` file in the destination directory and is published only after
  the complete response (exclusive hard link, or replacement with `--force`); a failure removes it and leaves an earlier destination
  untouched. The CLI is an API client and never reads SQLite.
- **Verified offline** with scripted providers, temporary homes/workspaces and
  direct store fixtures: both formats and headers; session and tree scopes;
  nested children kept in separate contexts; archived export without
  unarchiving; empty sessions; a missing snapshot labeled and left
  unreconstructed with no snapshot or event written; a running turn's durable
  partial output while it stayed alive; tool calls/results, reasoning, encrypted
  reasoning, raw parts and notices; fences and unusual Unicode; 5,010 events
  (more than one API page) complete and ordered; a read transaction that
  ignores a later commit while a fresh view sees it; repeated-export ordering
  and watermark stability; transport-header redaction with text preserved; no
  provider calls or database mutations; 401/400/404 responses; and CLI file
  creation, overwrite refusal, `--force`, stdout and failure cleanup. Full
  suite after delivery review: 213 tests; Ruff and `git diff --check` clean. No live provider or
  browser run was performed.
- **Limitations.** Export does not take the daemon's writer lock: it holds one
  WAL read snapshot for the duration of streaming, so a very large export keeps
  the WAL from checkpointing until it finishes (writers continue).
  Markdown omits wire events (JSON has the complete log) and intentionally does
  not re-clip output. The JSON document can be much larger than the stored
  database slice because events are included in full. There are no artifact or
  attachment records to export yet. The boundary labels stored state; it does
  not imply that interrupted or unfinished work was complete.

**Export delivery review.** Independently reran suite and lint. Non-forced CLI
publication now uses an exclusive hard link, eliminating the check/replace
race. Redaction applies only to actual wire transport headers, preserving tool
arguments and request bodies; cookie/proxy credential headers are now redacted
by the shared provider helper too. Markdown includes stored snapshot prompts
and schemas and does not claim unsent notices were already received. Response
teardown closes the read view even before the first chunk, and cancellation
waits for a worker before closing its generator/connection. Event pages iterate
rows without materializing all payloads. Six regressions cover these cases.
Final suite: 213 passing; Ruff and whitespace checks clean. No additional live
provider or browser run was performed.

**Instruction file (done in the foundation above; directory transitions added
above).** `AGENTS.md` is read directly
from the session's `directory` for both roots and children; ancestors and nested
directories are not searched. Its path and actual content are in the starting
context. A directory change loads the destination's
`AGENTS.md` and announces it as forward context without rewriting the previously
sent prompt. Absence is explicit in the context transition. Agent definitions
and this instruction file serve different purposes; changing directory does not
silently replace the selected agent definition.

**`ask` tool (before workspace controls, done 2026-10-27).** A task tool for
questions to the user: one or more questions, options (single or multi-select),
free text and optional previews. No timeout: wait until answered or the turn is
interrupted, then report cancellation in the result. Events: `question.asked` /
`question.replied`. Questions remain separate from approvals in Core; the
interface combines them as waiting for input. Implemented in this increment;
see the detailed record below before the workspace controls work.

**`ask` tool implementation (done 2026-10-27; delivered for review).**
- **Contract** (`nexus/questions.py`, `nexus/tools/ask.py`). `ask
  {questions: [...]}` takes 1-5 questions with stable IDs (`[A-Za-z0-9_.:-]`,
  1-64 characters, unique per request), non-empty text (≤ 2,000 characters),
  up to 10 options (`id`, `label` ≤ 100, optional `preview` ≤ 1,000), `multiple`
  and `allow_free_text`. No options means free text; with options free text is
  off unless allowed. A reply is a complete `{answers: [{question_id, selected?,
  text?}]}`; every question once, free text ≤ 2,000 characters. Unknown
  question/option IDs, duplicates, missing answers, invalid selection counts,
  disallowed text and malformed types are rejected before a pending request is
  created (tool arguments → tool error) or before a wait resolves (API → 400).
- **Waiting** (`Runtime.ask_user`). Validation happens first; only then is a
  `qst_…` request stored and `question.asked` published with session id, tool
  call id and the normalized questions. `ask` never asks for approval and is
  auto-approved. Its result is one JSON object keyed by question id with selected
  ids, labels and free text, so the tool result enters context exactly once.
- **API.** `GET /api/question`, `GET /api/session/{id}/question`, and
  `POST /api/session/{id}/question/{qid}` `{answers}` → 204; 400 invalid, 404
  unknown question/session, 409 already resolved, 401 without the token. A
  resolved request is tombstoned (bounded), so duplicate, conflicting or late
  replies cannot emit a second event or result.
- **Cancellation.** Interrupt, session/project deletion, daemon shutdown and a
  cancelled/timed-out enclosing `code` cell close pending questions and publish
  `question.replied` with `outcome: "cancelled"` and a reason. The wait future is
  cancelled; a late answer cannot resume work (409 while the session exists, 404
  once it is deleted). Interrupted tool
  calls now get an accurate result: a call that started says its result is
  unknown and it may have changed the workspace; only an unstarted call says it
  ran before the tool started. Completed results not yet appended are preserved.
- **Snapshots and trees.** `ask` is in `DEFAULT_TOOLS` (after `write`), so an
  agent that omits `tools` gets it and explicit lists/switches control it.
  Definitions and guidance come from the frozen snapshot; a stored snapshot from
  before `ask` stays without it. Child questions carry the child session id and
  appear on the parent's descendant event stream. Inside `code`, the nested ask
  uses the nested call id and the caller's restrictions; control tools are never
  exposed.
- **CLI and inspector.** The CLI renders questions, options (with previews) and
  collects selections or free text, then submits through Core. Permission
  prompts stay distinct. Ctrl-C/EOF abandons the prompt, stops the reader and
  interrupts the turn; the daemon closes the question. The inspector shows every
  `question.asked` with options, its answer or cancellation and live pending
  status, including questions nested in `code`.
- **Verified offline** with scripted providers and temporary homes/workspaces:
  free text, single-select, multi-select and multiple questions; mixed
  selections plus text; malformed requests and every invalid-answer class;
  pending-list and reply endpoints including 401/404; duplicate, conflicting and
  late replies; turn interrupt; session and project deletion; shutdown cleanup;
  child attribution and tree membership; disabled `ask`, explicit agent lists and
  stable pre-`ask` snapshots; nested code questions and cell-timeout cleanup; and
  the result appearing in context exactly once. Full suite after delivery review: 123 tests; Ruff and
  `git diff --check` clean. No live provider run was performed.
- **Limits.** Pending questions are in memory (a daemon crash loses them with the
  turn; startup recovery closes them with an explicit cancellation, Step 11) and
  there is no standalone "cancel this question" endpoint; cancellation
  comes from the turn/cell/session lifecycle. The inspector reads questions from
  the event log and pending endpoint, not a waiting-card UI; its script was
  syntax-checked, not visually re-run in a browser this increment. The CLI asks a
  second, optional text prompt when a selection question also allows free text;
  the API accepts both in one reply.

**Workspace delivery review.** Independently reran suite and lint. Fixed
worktree use checks for subdirectories and background jobs, reserved paths
through asynchronous deletion to block new users, and killed/reaped Git process
groups on cancellation. Worktree metadata now uses NUL delimiters (quoted,
Unicode and newline-containing paths remain intact); Git top-level paths omit
the output terminator. Activation describes only enabled tools. Pending moves
reload destination instructions at application, and tool moves applied during
teardown get a forward notice if no result carried it. Nine regression cases
cover these changes, including rejecting replacement of an already pending move. No live provider or new browser visual run was performed.

**Ask delivery review.** Independently reran the suite and lint. Fixed generic
clipping of valid structured answers (which could truncate JSON), a cancelled
future accepting a reply before waiter cleanup, and CLI input blocking the SSE
consumer. One serialized CLI input queue now handles questions/approvals while
events continue; resolved questions dismiss their reader. Stdin reading is
cancellable without background threads, including pipes, and does not prefetch
later lines. Four regressions cover large escaped answers, cancellation/reply
ordering, external resolution while typing and pipe-reader cancellation. No
live provider or new browser visual run was performed in this review.

**Workspace edits need no approval (done 2026-10-27; review pending).** Editing
inside the session's directory is allowed without approval, with no
permission-mode setting for that behavior. `edit` and `write` (replacement,
patch, and nested calls) are auto-approved in the daemon; bash approval is
unchanged. Path confinement and read freshness still apply.

**Enforced read-before-edit (done 2026-10-27; review pending).** Observations
live in the `file_observation` table keyed by session and resolved absolute
path: content `sha256` revision, total lines, model-visible line ranges, clipped
lines, whole-output clipping and the read call id. They persist across restart
and are never reconstructed from current contents. A direct, model-visible
`read` records one; directory listings, another session's read and a read
retained only inside a `code` kernel do not. Local replacement/hunk changes need
coverage of the original affected lines; overwrite, delete and move need
complete coverage; windows add up only within one revision. A clipped line is
not covered, and a whole-output-clipped read establishes no coverage. New files
need no read, but an existing `Add File` destination or an overwrite is refused.
Successful changes drop the affected observations. Nexus mutations share one
per-daemon asyncio lock, recheck the revision inside it immediately before
writing, and a multi-file patch validates every file before writing any. This
enforces the tool contract only: unrestricted `bash` and external processes can
still write outside it, and the final check-to-write window is not protected
against external writers. Full enforcement remains a prerequisite for claiming
a filesystem invariant (see Known gaps).

Verified offline with scripted providers, temporary homes/workspaces and real
files: unread rejection and read-then-edit; external and other-session changes;
identical timestamps with new content; partial and clipped windows; whole-output
clipping; windows merging only within a revision; overwrite, delete, move and
new-file collisions; a stale file rejecting an entire multi-file patch before
any write; session isolation and restart; nested reads not authorizing and
nested edits obeying the guard; approval-free edits under `permission: ask` with
bash still asking. Full suite after delivery review: 143 tests; Ruff and
`git diff --check` clean. No live provider run was performed.

**File freshness delivery review.** Independently reran the suite and lint.
Fixed repeated clipped reads incorrectly clearing the clipped-line marker;
staged direct-read observations until successful results enter stored context,
so a read cannot authorize an edit in the same provider tool batch. Preserved
CRLF bytes during local replacements and aligned patch/replacement coverage
with `read` for Unicode line separators. New-file creation uses exclusive opens.
Four regression tests cover these cases. Final suite: 143 passing; Ruff and
whitespace checks clean. No live provider run was performed.

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
cascades). No fork or revert: a session only moves forward. Archive and export
are done below.

### Step 11: event log follow-ups (done 2026-10-27; delivery reviewed)

Retention and size limits for the `event` table, resuming the live stream from
an event id, and durably recovering a crashed turn's partial state.

**Durable event identity and reconnect-safe replay (done 2026-10-27; delivery
reviewed).** The first half of Step 11: reconnect resumes the stream. That
increment did not implement retention, turn recovery, retries, idempotent
mutations, `/continue` or post-crash approval/question restore; turn recovery is
the increment below.

- **Identity.** The stored `event.id` is the durable event id: an autoincrement
  row id, never reused, stable across daemon restarts. A new `log` table holds
  one `generation` (`log_<hex>`) for the database, created on first open and
  kept afterwards; a new/deleted/replaced database gets a new generation, a copy
  keeps it. A cursor is `<generation>:<id>` and ordering is ascending id. The bus
  `seq` is only the current daemon's counter and is documented as **not** a
  cursor. `/api/info` reports `log.{generation,head,head_cursor,floor}`.
- **Migration.** An older database is migrated in place on open: the `event`
  table is rebuilt with a nullable `session_id` (for globals), every row id and
  the autoincrement sequence are preserved, and the `log` row is created. Stored
  event paging (`?after=`) and export ids keep working unchanged.
- **Persist before publish.** `Bus.publish` commits through the attached store
  before enqueuing to any subscriber and stamps the durable `id`. A storage error
  propagates — no subscriber sees an event that cannot be replayed. An event
  whose session row is already gone is not stored or delivered; its deletion
  event covers it.
- **Global lifecycle and deletion.** `project.created/updated/deleted` are now
  stored with `session_id: null`, as are `session.deleted` tombstones
  (`{"id": …}` only). Deleting a session publishes one tombstone per deleted
  session (root first), deleting a project publishes one per session before
  `project.deleted`. Messages, events, snapshots and observations still cascade
  with the session; the tombstone carries only the id needed to synchronize.
- **Atomic replay boundary.** `GET /api/event` accepts `?cursor=` or
  `Last-Event-ID` (the query wins). The server validates the cursor, registers
  the live subscription and reads the durable head as one lock-protected step,
  then replays `(cursor, head]` in 500-row pages, then continues live while
  skipping ids at or below the head. Events published during catch-up are queued,
  so they are delivered exactly once at the handoff and in order. No catch-up cap
  yet.
- **Filtering.** Session streams include the selected session, descendants and
  global events. Membership is maintained from `session.created` (parent_id) and
  the deletion tombstones, not only from live relationships, so descendants
  created during replay are included and deleted subtrees stop matching. Numeric
  gaps are documented as normal.
- **Failures.** Malformed cursor → 400 `invalid_cursor`; another generation →
  409 `cursor_generation`; ahead of head → 409 `cursor_ahead`; below the floor →
  410 `history_unavailable` (floor is 0 until retention). Every one carries
  `resync: true` plus generation/head/head_cursor/floor/floor_cursor. Slow
  subscribers get one `bus.overflow` frame (not stored, no id) with
  `dropped_after`, `generation` and `head`, then the stream ends.
- **Clients.** `Client.events(cursor=…)` sends `Last-Event-ID`, yields durable
  ids/cursors and raises `ResyncRequired` (status, code, generation, head,
  head_cursor, floor) for 400/409/410. The inspector uses a fetch-based stream
  instead of `EventSource`: it resumes from its cursor, deduplicates by durable
  id, buffers events during a session load, and resyncs explicitly on cursor
  failures or overflow. Reconnecting never resubmits a prompt.
- **Exports.** The JSON boundary adds `log_generation`; the response adds
  `x-nexus-log-generation`; the existing `event:<id>;seq:<seq>` header is
  unchanged.
- **Verified offline** (`tests/test_eventlog.py`, scripted/no provider): restart
  preserving ids and generation while `seq` resets; a replaced database
  rejecting the old generation; commit-before-delivery; publication during
  replay and at the live handoff delivered once; 5,010-event paged replay;
  filtered streams with legitimate gaps; descendant creation/deletion during a
  live stream plus filter membership from recorded creates/tombstones; global
  lifecycle and tombstone durability; malformed, foreign,
  future and below-floor cursors; query/header precedence; overflow at the bus
  and stream; a persistence failure that publishes nothing; disconnect cleanup;
  the raw ASGI framing of `server.connected` and durable `id:` lines; stored
  paging and export ids; and migrating the previous event schema. A real
  `nexus serve` in an isolated home was also exercised with curl: `server.connected`
  and durable `id:` frames, a live `project.updated` published during the stream,
  a restart that kept the generation and head and resumed from the saved cursor
  with a continuing id (while `seq` restarted), and a replaced database returning
  409 `cursor_generation` with the resync head. Full suite:
  241 tests; Ruff and `git diff --check` clean. No live provider or browser run
  was performed; the inspector script was syntax-checked with `node --check`.
- **Limitations.** No retention: the log grows without limit and the floor stays
  0. Automatic retries and idempotent mutation requests are still open. Turn
  recovery and manual continuation are implemented below; a crash during
  recovery is repeatable.
  `bus.overflow.dropped_after` is the last
  live event enqueued for that subscriber, which can be ahead of what it applied
  during replay; clients resume from their own cursor. Archive transitions
  remain root-log-only (existing limitation).

**Durable turn recovery (done 2026-10-27; delivery reviewed).** The second half of
Step 11: a restart reconciles turns a crash left unfinished. It does not
implement automatic retries, mutation idempotency, retention, web tools, skills
or MCP. Manual continuation is the increment below.

- **Execution records.** Three tables, all cascading with the session. `turn`
  (`id`, session, `status` running/completed/interrupted/failed, start/finish,
  steps, `start_seq`, error, `recovery` none/pending/done, notice id, data) is
  written before a prompt's task starts. `turn_request` records each provider
  request (step, status started/completed/interrupted/failed, log watermark,
  assistant message id, stop, response id, error and the completed `PartDone`
  parts). `turn_tool` records each call as `saved` (in a saved assistant
  message, never started), `started`, `finished` (result durable, not in
  history) or `inserted` (in a saved tool message), with output, error and any
  read observations. A `message.event_id` links a saved message to its
  `message.added` event.
- **Contract.** `Runtime.recover()` runs from `make_runtime` before the app can
  accept a request. It marks unfinished turns interrupted (or completed when a
  durable `turn_end` proves the loop finished), leaves sessions idle, and never
  calls a provider or runs a tool. A response that durably
  reached `Done` is restored exactly from its completed parts; an interrupted
  stream is never turned into an assistant message. Durable tool results are
  preserved and inserted once; started calls are unknown unless a pending or
  denied approval proves they did not run; saved calls did not run. Pending
  questions and approvals receive explicit cancellation outcomes, and old ids
  cannot resume the work. Kernels and background jobs are reported lost, with
  no PID-based killing. Parent and child sessions are recovered independently;
  archive state is untouched.
- **Partial output.** Deltas, completed `part_done` items and `wire_event`
  records remain in the durable log for inspection, with `origin`/`raw`. They
  are not replayable context: a cut-off response cannot be presented as
  complete, and recovery never invents complete arguments or encrypted
  reasoning. The new `[Nexus recovery: interrupted turn]` user message is
  labelled, reports counts, classifications and losses, and is the only new
  context besides restored results/messages.
- **Transactions and repeatability.** An assistant message, its request link
  and its calls' `saved` rows are one transaction; a tool message and its
  results' `inserted` rows are one transaction; a notice and its turn marker
  are one transaction. Each recovery event is checked in the durable log before
  publication, and messages saved before a crash but not announced are
  announced once. Recovery can itself be interrupted at any step and finishes
  on the next start without duplicates. Older databases gain the tables on
  open. A session whose saved context ends in tool calls and has no turn record
  is treated as a legacy interrupted turn: one labelled turn is created and its
  calls are closed as unknown (never "did not run" and never a fabricated
  result); normal old conversations and all existing event ids are untouched.
- **Events.** `turn.recovered` (structured outcome), a `session.status` idle
  with `reason`/`turn_id`, `question.replied`/`permission.replied`
  cancellations, and ordinary `message.added` events. `permission.replied`
  now carries `outcome` (`allowed`/`denied`/`cancelled`) and `session_id`.
  Existing event ids, log generation, deletion tombstones and archive behavior
  are unchanged.
- **Verified offline** (`tests/test_recovery.py`, scripted/no provider):
  accepted-before-request; interrupted streaming with no fabricated message;
  a completed response restored from parts; a durable `turn_end` recorded as
  completed without a false interruption notice;
  started-unknown, finished-preserved
  and saved-unstarted calls; a mixed multi-call batch; pending approval and
  question cancellation and late-reply refusal; an answered question restored
  from its durable answer; nested `code` with lost kernel and orphan background
  job; parent and child together without notification; archived and idle;
  recovery interrupted at every named step then repeated; repeated startup with
  no duplicate message or event; a message saved before its event announced
  once; migration from the pre-increment schema with a
  normal conversation left untouched; legacy dangling context labelled unknown
  once; durable replay of `turn.recovered`; no provider or tool
  call during recovery; and an isolated subprocess daemon kill/restart smoke
  test with a temporary `NEXUS10_HOME` and workspace.
- **Limitations.** No retry or idempotent mutations; manual continuation is the
  next increment. A background
  job that outlives an orderly turn and whose session had no interrupted turn
  is not reported individually after a restart (its record and output are
  gone). Kernels have no state recovery. There is no process supervision. A
  crash between saving a read result and committing its observation can lose
  the observation; recovery restores only observations recorded with the saved
  result, never by reading the file again. The crash windows are reconstructed
  deterministically from durable rows in tests, not by killing a live provider
  stream; no live provider run was performed. Full suite after implementation:
  275 tests; Ruff and `git diff --check` clean.

**Reconnect contract.** Durable cursors survive daemon restarts (or explicitly
identify a changed log generation). Catch-up and live subscription share an
atomic boundary: no lost events between them. Replayed events have stable ids
and clients deduplicate them. Define retention expiry with an explicit
cursor-expired response that requires a fresh snapshot and cursor. Snapshots
include a watermark so subsequent events can be applied without races.
Filtered streams naturally skip unrelated global sequence numbers: a numeric
gap alone is not evidence of loss. Overflow, expired cursors or an explicit
continuity failure trigger resync. Reconnect restores queues and session lists
as well as visible text; it restores pending questions and approvals only by
showing their durable cancellation after a restart (no in-memory wait survives).

**Mutation contract.** Client request ids make retried prompts and other
mutations idempotent, with a defined deduplication lifetime and replayed
response. Concurrent clients get deterministic conflicts: approval/question
answers resolve once; stale queue edits and settings updates use revisions.
Document ordering of steer, queued prompts, setting changes and interrupts.
Verify disconnect during send, catch-up/live races, filtered streams, restart,
retention expiry and competing clients.

**Manual continuation (`/continue`) (done 2026-10-27; delivery reviewed).** The
third part of Step 11: an explicit client action resumes generation from the
stored context. It does not implement automatic retries, mutation idempotency,
retention, web tools, skills or MCP.

- **API.** `POST /api/session/{id}/continue` is authenticated and starts one
  normal turn with no user text. It returns 202 with `{status: "busy", turn_id,
  origin: "continuation", continuation, reconciled}`. It refuses a running turn
  (409), an archived root tree (409 with the unarchive command), an empty
  conversation (400 naming the prompt endpoint/CLI), an unknown session (404), a
  missing token (401), and a session with an unfinished durable turn (409;
  restart so startup recovery reconciles it). Reconnect, session reads and
  daemon startup never continue by themselves.
- **Loop and lifecycle.** `Agent.turn` takes either text or the explicit
  `NO_USER_MESSAGE` marker. Ordinary prompts add exactly one user message;
  continuation adds none. Runtime's `prompt` and `continue_session` share
  `_begin_turn`, so both write the durable turn before the task exists, publish
  busy before running, and use the same execution, interrupt, approval, question
  and recovery paths. The current effective model, effort, tools, permission and
  directory apply; new requests, responses and tool calls are recorded normally.
- **Provider-valid context.** `nexus/continuation.py` scans saved messages for a
  tool call with no result before the request. It closes only calls whose later
  messages are tool results; a dangling call followed by conversation is refused
  rather than closing it out of order. It uses durable evidence: a completed
  result is preserved, a started call is labelled unknown and never re-run, a
  saved call did not run, pending/denied/cancelled approvals and answered or
  cancelled questions keep recovery's outcomes, and a call with no execution
  record in a legacy conversation is labelled unknown. One tool message and one
  labelled `[Nexus recovery: continuation reconciled interrupted tool calls]`
  user notice are committed in one transaction, so repeating continuation adds
  nothing. Partial streams stay diagnostic-only in the event log and are never
  turned into arguments, reasoning or a completed response. Startup recovery is
  never run for other sessions.
- **Provenance.** The new turn's durable `data` records `origin:
  "continuation"` and `continuation.predecessor` (`turn_id`, `request_id`,
  status, request status, step, stop, `link`: request/turn/none, legacy note). It
  names the preceding attempt when one exists and says `link: "none"` for a
  legacy conversation without execution records. A crash during continuation
  keeps this data; `turn.recovered` exposes `origin` and `continuation`, and its
  notice names the predecessor. The durable `session.status` busy/idle events
  carry `origin`, `turn_id`, `continuation` and (when repaired) `reconciled`.
  Provenance is API/event metadata, never an extra model-visible user message.
- **CLI.** Chat supports `/continue`; `nexus session continue ID` is the
  standalone command. Both subscribe to `/api/event?session=` before posting,
  render the normal event stream (including children), and handle questions and
  approvals through the shared input queue. A network failure while posting is
  reported as ambiguous and is not retried. `--wire`/`--wire-file` work for the
  standalone command.
- **Verified offline** (`tests/test_continuation.py`, scripted providers):
  the exact first request equals the stored messages with no synthetic user
  message; ordinary prompts are unchanged; continuation after a provider
  failure, a clean interrupt and startup recovery; an earlier completed turn;
  completed tools are not re-run; a finished dangling result is preserved; a
  started call stays unknown; new calls from
  the continued model write normally; current model/effort/tools/directory
  apply; busy, archived, empty, unknown and unauthenticated responses;
  parent/child independence; provenance in `turn.data`, the busy event and the
  endpoint; crash-during-continuation recovery keeps the link; reconciliation
  of saved and legacy calls once; CLI slash, standalone, subscription ordering
  and ambiguous POST failure. Full suite: 296 tests; Ruff and
  `git diff --check` clean. No live provider or browser run was performed.
- **Limitations.** Pending questions/approvals a client saw before a daemon
  crash are cancelled, not restored; continuation after recovery starts a new
  turn and does not answer them. Automatic retries and idempotent mutation
  request ids remain separate. A crash during the continuation POST leaves an
  ambiguous client result by design; clients inspect the session rather than
  repeating the POST. The reconciliation notice is user-visible context added
  only when repair is needed; a normal continuation adds none.

#### Step 11 follow-ups: implementation and delivery record

This records the execution plan for the completed follow-ups to Step 11. Durable replay,
startup recovery and manual continuation are implemented and delivery-reviewed;
keep their existing tests and contracts. The instructions below are now
implemented (2026-10-27); the completion record follows 11.7. Steer/queue
implementation stays in Step 12a; artifact storage follows Step 11.

**11.1 — Freeze the failure and ordering contracts. (done 2026-10-27).**

The reviewed contract table and supported-route matrix are published in
[`docs/api.md`](docs/api.md#mutations-retries-continuation-and-reconnect).

1. Read `docs/api.md`, `docs/daemon.md`, `docs/loop.md`,
   `docs/llm-providers.md` and `docs/stream-events.md`. Trace `server.py` →
   `runtime.py` → `loop.py`, and `store.py`, `bus.py`, `eventlog.py`,
   `recovery.py`, `continuation.py`, `client.py` and the CLI input/event loop.
   Run the existing suite and lint as a baseline.
2. Add a contract table to this section before coding: distinguish a client
   retry of the same mutation, a provider request retry within one turn,
   event-stream reconnect, and explicit manual continuation. Identify which
   operation owns each timeout, retry budget and durable record. None of these
   actions implicitly answers a question or approval, or restarts a recovered
   turn.
3. Inventory every mutating HTTP route. For each, specify request-id support,
   revision checks, response replay, deletion behavior and crash reconciliation.
   Start implementation with prompt/continue, then cover the rest; publish the
   supported-route table so clients never assume an unsupported POST is safe.
4. Specify the order of accepted mutations, settings changes, directory moves,
   interrupts and turn cleanup. Use durable acceptance order and explicit safe
   application boundaries. Document the future insertion points for steer and
   queue without implementing those Step 12a features.

Completion: a reviewed contract and route matrix with concrete status codes,
request/response examples, retry limits and expiry rules; no ambiguous promise
of exactly-once external side effects.

**11.2 — Add durable mutation request IDs, beginning with prompt/continue. (done 2026-10-27).**

1. Define one bounded `Idempotency-Key` header, scoped to the local API and log
   generation. Fingerprint the method, normalized target and validated semantic
   body; do not include credentials. Choose and document the key syntax, maximum
   length and deduplication lifetime before migration. A proposed initial
   lifetime is seven days after terminal resolution; unresolved entries do not
   expire. Expired keys are no longer safe to retry automatically.
2. Add a migration for a mutation journal: key, fingerprint, operation, target,
   accepted time, state, result/resource/turn link, terminal status/body and
   expiry. Bound retained response sizes. The same key with a different
   fingerprint returns 409; concurrent identical requests share one acceptance
   and the same result, never two turns. Resolve a known key before looking up
   a target that its original request may have deleted.
3. For prompt and continue, atomically record acceptance, the turn and enough
   input to reconcile a crash before scheduling its task. Link the request to
   exactly one turn. Replay the original acceptance and turn id even when that
   turn has since finished or been interrupted; a replay never schedules work.
   A crash after acceptance and before task creation is recovered visibly,
   rather than leaving an unresolvable pending journal entry.
4. Preserve existing clients initially: requests without a key retain the
   current single-attempt behavior. Add keyed requests to `Client` and CLI;
   generate the key once per user action and retain it across transport retries.
   Return the key and stable operation/turn link in acceptance metadata, never
   as an extra model-visible user message.
5. Test concurrent duplicates; changed payloads; response loss before/after
   commit; crash before task creation; restart and replay; a completed turn;
   archive/delete races; and transactional migration rollback. Assert one user
   message, one turn and no duplicated provider/tool execution.

Completion: losing a prompt/continue HTTP response can be resolved with the
same key and cannot create a second turn.

**11.3 — Cover remaining mutations and competing clients. (done 2026-10-27).**

1. Extend the journal to session/project creation, updates/deletion, archive,
   workspace controls, interrupts and question/approval replies, according to
   the route matrix. Preserve bounded tombstones for deleted targets and
   resolved waits through the deduplication lifetime. Distinct contradictory
   replies still conflict; replaying the same accepted reply emits no event and
   releases no tool twice.
2. Add monotonic revisions to mutable project/session configuration and expose
   them in reads and events. Define an expected-revision precondition for
   updates; reject stale updates with 409 and the current revision before any
   change. Serialize competing mutations at the owning resource boundary;
   settings and pending directory changes apply at the documented call boundary.
   Retried keyed updates replay their result before rechecking the revision.
3. Treat Git operations and other external effects separately from SQLite:
   persist intent before execution and durable evidence afterwards. On a crash,
   reconcile only from verifiable evidence; otherwise return a stable unknown
   outcome and require inspection. Never blindly repeat an ambiguous worktree
   create/delete. The journal does not make external side effects transactional.
4. Test two clients changing settings, duplicate/conflicting answers, deletion
   during execution, interrupt versus cleanup, and crashes around each external
   effect. Verify event ordering and unchanged historical context.

Completion: all mutation routes have an implemented, documented replay/conflict
policy, including explicit unknown outcomes for ambiguous external effects.

**11.4 — Add bounded, visible provider retries. (done 2026-10-27).**

1. Inspect provider errors and preserve structured retry information (HTTP
   status, retry-after and transport phase), with redacted diagnostics. Choose
   a shared policy rather than provider-name branches. Proposed defaults:
   three attempts total, exponential backoff with jitter starting at one second,
   at most 30 seconds per wait and 60 seconds total elapsed retry time.
2. Automatically retry only explicitly transient failures before any response
   stream event has been received: connection failures, 429 and selected
   502/503/504 responses. Honor a valid Retry-After within the remaining budget;
   if it exceeds the budget, stop and report it. Authentication, validation,
   cancellation, persistence failure and unknown errors are terminal. A
   pre-stream retry can still duplicate provider computation/billing; document
   that limit rather than claiming provider-side exactly-once behavior.
3. Persist each attempt under its turn/step with an attempt id and outcome.
   Keep every wire request, response and SSE event, and publish retry
   scheduled/started/exhausted events with reasons and delay. These are labelled
   execution diagnostics, not invented conversation messages. An attempt does
   not consume another agent step or append the user's message again.
4. Once any stream event arrives, stop on failure and retain partial output as
   diagnostic-only evidence; use existing recovery/manual continuation contracts.
   Never automatically rerun a tool, outer `code` call, background command,
   approval or question. A daemon restart never resumes a pending retry.
5. Make backoff interruptible; deletion and shutdown cancel it promptly. Specify
   whether a settings change cancels/rebuilds a retry or waits for the next
   logical request, so the retried request cannot silently change contents.
6. Test classifications, exact budgets and Retry-After using a fake clock;
   interruption during backoff; attempt persistence; stream failure after its
   first event; and a completed tool followed by a provider failure. Assert no
   tool replay, duplicate message or hidden wire attempt.

Completion: transient pre-stream failures retry within a fixed budget, visibly;
partial streams and tool side effects are never automatically replayed.

**11.5 — Reconnect clients and add a consistent resync snapshot. (done 2026-10-27).**

1. Extend the Python client and CLI to retain the last applied durable cursor,
   reconnect with bounded backoff, and deduplicate by generation/event id.
   Advance the cursor only after applying an event. A reconnect never resends
   a prompt; only the mutation policy can retry its keyed HTTP request.
2. Add a consistent API snapshot plus watermark for the client state needed to
   rebuild project/session lists, selected session context, running turns and
   pending questions/approvals. Read durable state in one SQLite snapshot and
   coordinate live wait/status capture with runtime mutation boundaries. Use a
   bounded selected-session view and pagination contract for large histories.
   Apply subsequent events strictly after that watermark.
3. Handle overflow, generation mismatch, cursor-ahead and history-unavailable
   by fetching that snapshot and resubscribing; never infer loss from numeric
   gaps in a filtered stream. Restore waiting UI only from current pending
   requests; restart cancellation events dismiss old waits. Resync must not
   duplicate a CLI reader or submit a stale answer.
4. Test disconnect during send, before/after applying an event, during replay,
   and while answering a question; catch-up/live races, descendant creation,
   archive/deletion, restart and replacement of the database. Include an
   inspector resync check so both existing clients obey the same contract.

Completion: a client can reconnect or rebuild its view without missing context,
duplicating input or starting a turn.

**11.6 — Implement retention without destroying recovery evidence. (done 2026-10-27).**

1. Audit every event-log dependency in recovery, continuation, exports,
   inspector filtering and mutation reconciliation. Move required evidence
   into durable lifecycle records or pin it before deleting events. An idle
   session can still have a dangling call or live background job; idle alone
   is not proof that its events are disposable.
2. Choose configurable age and logical-byte budgets, maintenance cadence and
   batch size before coding; document defaults and the difference between
   retained payload bytes, SQLite file size and WAL size. Do not promise a hard
   disk ceiling when active recovery evidence must be retained. Report pressure
   and pinned evidence explicitly, without silently dropping incoming events.
3. Prune only a contiguous global prefix and advance the floor atomically.
   Define floor as the last removed event id: cursor == floor can resume;
   cursor < floor returns 410 `history_unavailable`. Preserve generation and a
   durable head high-water mark even when every retained row is deleted;
   `max(event.id)` alone is no longer sufficient. Never reuse ids.
4. Coordinate pruning with paged replay: pin the replay range for a bounded
   period or terminate it with an explicit resync outcome before removing
   unread rows. Do not allow paging to silently skip a deleted interval.
   Preserve descendant membership/tombstone information needed for filtering.
5. Retain messages, snapshots and model context independently of diagnostic
   event expiry. Inspector, per-session event endpoints and exports must state
   that earlier events expired, with the retained boundary; remove claims of
   complete wire history when retention has removed it. Full JSON exports still
   use one read boundary and include all events retained at that boundary.
6. Define separate retention for terminal turn/request/tool rows and mutation
   journal entries. Keep evidence needed for dangling calls, recovery and the
   deduplication lifetime; remove linked records in dependency order. Pruning
   never changes a provider-valid conversation or permits an old key to be
   retried automatically after its guarantee expires.
7. Test exact floor boundaries, empty retained logs, restart/high-water marks,
   pruning during replay/export, pinned recovery evidence, historical filtering,
   expiry of request keys and crash/rollback during pruning. Exercise a large
   fixture to verify bounded batches and that ordinary turns remain responsive.

Completion: retention has explicit limits and observable exceptions, expired
history forces resync, and no recovery or continuation decision loses evidence.

**11.7 — Verify and deliver Step 11. (done 2026-10-27).**

1. Run focused regressions for each increment, then from `nexus-core/` run
   `uv run pytest -q` and `uv run ruff check nexus tests`; run
   `git diff --check` at the repository root. Add tests for contracts and crash
   boundaries, not mirrors of helper implementations.
2. Run an isolated daemon/client smoke scenario with a temporary
   `NEXUS10_HOME` and workspace: submit a keyed prompt, lose its response,
   reconnect, interrupt/restart, inspect recovery, continue explicitly, expire
   history and resync. Use scripted providers for deterministic faults. Record
   any separate live-provider or browser verification honestly.
3. Update `docs/api.md`, `docs/daemon.md`, `docs/loop.md`, provider/event docs,
   inspector docs and `docs/development.md` as their behavior changes. Add a
   ledger row when behavior deliberately differs from old Nexus. Mark each
   increment done only with its verification and remaining limitations.
4. Reconcile stale Step 11 review labels and Known gaps against actual delivery
   evidence. Keep kernel isolation, process supervision, artifacts, steer/queue
   and automatic context handoff in their own planned increments.

**Step 11 remaining work delivered (2026-10-27).** All seven increments are
implemented and reviewed together with the existing replay, recovery and
continuation contracts. This is not an automatic-retry or exactly-once claim for
external side effects.

- **Contracts (11.1).** `docs/api.md` now has the four-action table (client
  mutation retry, provider retry, reconnect, manual continuation) with owner,
  budget and durable record; the supported mutation route matrix with replay,
  revision and crash behavior; and explicit insertion points for Step 12a steer
  and queue (a prompt while busy remains 409). No action implicitly answers a
  question/approval or restarts a recovered turn.
- **Mutation journal (11.2–11.3).** `mutation` stores key, fingerprint,
  operation, target, acceptance, state, status/body, turn/resource and expiry.
  `Idempotency-Key` is 1–128 characters, scoped to the local API and log
  generation; the fingerprint covers method, target, canonical body and revision
  preconditions, never credentials. Same key/body replays the original response (including after the
  target was deleted); a changed payload is 409 `idempotency_key_reused`;
  concurrent identical requests share one acceptance. Prompt and continue write
  the journal row in the same transaction as the `turn`, so a crash leaves both
  or neither; replay returns the original turn id and schedules nothing. A
  pre-acceptance failure (such as busy) is not journaled, so the key can be used
  after the conflict clears. Accepted responses are bounded at 64 KiB; larger
  results replay as a bounded record that points at the resource.
- **Revisions (11.3).** Projects and sessions have a monotonic `revision` in
  reads and updates. `If-Match` or `expected_revision` rejects stale updates with
  409 `revision_conflict` before any change; keyed updates replay before that
  check. Git worktree create/delete persists a pending intent before execution;
  a crash turns it into **409** `mutation_outcome_unknown` (stable and expiring),
  never a blind repeat. External effects are still not transactional.
- **Provider retries (11.4).** Only pre-stream transport, 429, 502, 503 and 504
  failures retry, within a shared policy (3 attempts, jittered backoff from 1 s,
  30 s per wait, 60 s per step; environment-tunable). `Retry-After` is honored
  when affordable. Each attempt is a `turn_attempt` row and a
  `retry_scheduled`/`retry_started`/`retry_exhausted` event. Auth, validation,
  cancellation, persistence, unknown and post-stream failures are terminal.
  Partial streams, tools, code cells, background commands, approvals and
  questions are never replayed. A restart does not resume a pending retry; the
  request body is unchanged across attempts.
- **Client reconnect and snapshot (11.5).** `GET /api/snapshot` reads projects,
  sessions, running turns, pending waits and an optional bounded selected-session
  message view at one watermark, with the head read in the same SQLite read
  transaction. `Client.watch` advances its cursor only after an event is
  consumed, reconnects with bounded backoff, deduplicates by durable id, and on
  cursor failure or overflow fetches the snapshot and resyncs after its head.
  The CLI uses `watch`; it never resubmits a prompt on reconnect. Prompt and
  continue POSTs carry one idempotency key per user action and reuse it for the
  client's own transport retries. The inspector's fetch stream already reloads
  state on cursor failures/overflow and now names an expired event boundary.
- **Retention (11.6).** `nexus/retention.py` removes only a contiguous global
  prefix (default 14 days and 256 MiB of payload, hourly), advances `log.floor`
  atomically, and preserves a head high-water mark so ids are never reused. A
  cursor at the floor resumes; below it gets 410 `history_unavailable`. Prefix
  each maintenance pass removes at most 5,000 events in one transaction. In-flight
  replay ranges are pinned; unfinished turns/requests, open waits and
  live jobs protect their sessions' events. Messages, snapshots and model
  context are retained independently. Terminal turns keep 200 recent rows per
  session plus recovery evidence; mutation entries expire seven days after
  terminal resolution. A global `log.retention` event reports each pass. The
  inspector shows the retained boundary rather than claiming a complete wire
  history.
- **Verification (11.7).** Focused regressions cover keyed prompt/continue
  replay, changed payloads, a crash before task creation, deletion replay,
  revision conflicts, retry classification/budgets/Retry-After/attempt
  persistence/no tool replay, snapshot watermark/pending state, reconnect
  deduplication/resync, exact floor boundaries, head high-water marks, pins,
  protected recovery evidence, byte budgets and lifecycle pruning. See
  `tests/test_mutations.py`, `test_retry.py`, `test_retention.py` and
  `test_client_snapshot.py` (including concurrent identical keys, an interrupted
  retry backoff, external-effect unknown outcomes and bounded large pruning). The
  full suite after delivery review (345 tests) and lint are clean. No live provider
  run, browser visual run or real container/network fault was performed for
  this increment; the daemon smoke used an isolated `$NEXUS10_HOME` and scripted
  or no-provider paths.

**Remaining limitations.** An ambiguous external Git effect is not reversible
and is reported unknown. Provider retries can duplicate provider-side
computation/billing; they are not provider-exactly-once. A keyed continuation's original acceptance can be replayed safely. Ordinary
mutations persist intent first; if a crash loses their returned result, replay
reports `mutation_outcome_unknown` and requires inspection. Retention protects evidence it can
identify durably; an arbitrary external process's use of old events is outside
that contract. Steer/queue, session drafts, artifacts, skills/MCP and automatic
compaction remain later steps.

Step 11 is complete when keyed mutation replay, concurrency conflicts, bounded
provider retry, client reconnect/resync and retention are implemented and
reviewed together with the existing replay/recovery/continuation guarantees.

**Step 11 delivery review (2026-10-27).** Independently reran the full suite
and lint. Fixed ordinary mutations' response-journal crash window by recording
intent before execution; an unrecorded result now replays as a stable unknown
outcome instead of repeating a create/delete or wait reply. Fingerprints include
revision preconditions, and per-key locks retain queued waiters safely. Fixed
byte-budget pruning to retain the newest fitting suffix independently of age,
limited each event deletion transaction to 5,000 rows, protected dangling tool
and retained mutation evidence, and released replay pins at the live boundary.
Client overflow now forces snapshot resync; reconnect preserves the initial head,
exhausts repeated empty streams, closes old transports, and refreshes rotated
credentials only for locally discovered daemons. CLI resync restores only its
selected tree's waits/status. Provider retries freeze their request across
settings changes and recheck the elapsed budget after backoff. Regression
coverage includes these crash, retention, reconnect and retry boundaries.
Final suite: 345 passing, including the isolated daemon restart smoke; Ruff and
whitespace checks clean. No live provider or browser visual run was performed.

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
- **Archive, search.** `archived` on sessions is done (Step 10). Search over
  session titles and user message text only (an SQLite FTS5 index of exactly
  those fields) remains.
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
- Required scope: reviewed Steps 8–9 and 9a, frozen context/control decisions, durable
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

Build the Ratatui TUI (`nexus-tui/`) first, then the shared NSH web and desktop
app (`nexus-nsh/`); adapt the same app to iOS after the desktop interaction is
stable. Both speak only the Core HTTP API. [INTERFACE_PLAN.md](INTERFACE_PLAN.md)
is the shared product specification; this section is the implementation order.

Progress (2026-10-27, current increment):
12.1 is delivered as `interface/` (`capability-matrix.md`,
`reference-inventory.md`, `manifest.toml` + `check_manifest.py`). 12.2 is
delivered through item 3 (`theme.toml`, `check_theme.py`, the "Design language"
section; gallery captures are partial). 12.3 is complete and 12.4 is largely
delivered in `nexus-tui/`: supervisor and state, both sidebars and drawers, the
palette/settings/alerts shell, the waiting card and composer, and the timeline
(starting context, turns and footers, folding, reasoning, Markdown, tool rows
with diffs/output/raw, child cards, notices, diagnostics). Selecting a session
loads all messages, retained diagnostics and the snapshot (paged to the end,
keeping the newest 20,000 events and announcing retention, the cap or a failed
page; live messages are merged, and every resync reloads it); model/effort/agent
pickers, export, directory change, copy actions, turn navigation and inspector
links are wired. The TUI has 113 tests, `cargo clippy --all-targets -D warnings`
passes and `cargo fmt --check` is clean.

12.5 is partial: `nexus-nsh/` has the same shell and timeline against a real
Core through its host proxy, with the same timeline, catalog pickers for
model/effort/agent and export, built from the shared manifest/theme; its 13 browser
interaction tests pass (`nsh test --target web`), the host proxy's hardening
tests pass (`npm run test:host`), `nsh check` is clean, and the
packaged app builds. `e2e/` captures 390x844, 1024x768 and 1440x900 in dark and
light.

Core: `/api/info` now reports `api_version: {major, minor}` (clients halt on a
major mismatch). The remaining matrix gaps still need Step 12a decisions
(approval choices beyond allow/deny, message paging, durable usage totals, write
previous content, interrupt-cancel event for approvals, agent `mode` over HTTP,
catalog refresh over HTTP, project-level worktree list). Still client work for
12.4: Files/editor and Git (also blocked on Core file endpoints), worktree UI,
settings editing beyond theme, and search; steer/queue, btw, handoff, one-turn
sessions, attachments, dictation, skills/MCP and sign-in over the API remain
Core or later increments.

**Design brief.** Use `../nexus` as the primary TUI reference: a recognizably
similar working environment, substantially more polished. Give Nexus a coherent
design language across terminal, browser and desktop. This is substantial design
work with its own review milestones, not a final theme pass. Web and desktop
follow the TUI's information architecture, terminology, settings order and
control locations as closely as possible. They may show more information and
richer previews inside the same structure. Additional information must come from
Core and be labelled as context or inspection; never invent model context.

#### 12.1 — Audit references and freeze the shared interface map

1. Read all of `INTERFACE_PLAN.md` and the matching Core API, event, tool and
   inspector docs. Make a capability matrix: API ready, Core work required,
   client work required, or explicitly deferred. Required flows blocked by
   Steps 11, 11a or 12a need a Core increment, not client-owned Nexus state.
2. Inspect the old TUI deliberately: `../nexus/rust/tui/src/render/` (chrome,
   context, sessions, dialogs and feedback), `sidebar.rs`, `details_sidebar.rs`,
   `settings_page/`, `markdown.rs`, `editor.rs`, `../nexus/rust/widgets/src/`
   (theme, geometry, focus, controls and glyphs), and
   `../nexus/docs/ratatui-parity.md`. Use its screenshots and runnable app for
   visual comparison when available. Record what to retain, improve and omit.
   Old Nexus's AGENTS.md, host bridge, configuration and state model do not
   apply; never read `~/.nexus`. Copy code deliberately with attribution and a
   ledger entry for changed behavior.
3. Freeze a screen map and command/settings manifest shared by both builds:
   stable IDs, labels, order, placement, keybindings, enabled conditions and
   unavailable reasons. Share data and fixtures; choose shared Rust logic only
   where it actually reduces duplication. Do not couple the renderers.
4. Fix navigation anchors in both clients: left sidebar toggle at the leading
   edge of the workspace header, session title/breadcrumbs in the centre,
   right sidebar toggle at the trailing edge. Keep both toggles available when
   their sidebar is closed. Left tabs are Sessions, Files, Projects in that
   order; Settings stays at the bottom of the left sidebar and in the palette.
   Narrow layouts turn sidebars into drawers while retaining those anchors.
5. Settings order is Appearance → Providers → Models → Agents → Tools → Skills
   → MCP → Context → Dictation → Keybindings → Notifications → About. Within
   each section, shared settings retain the same relative order. Client-only
   rows, such as NSH font size, are labelled and inserted in the appropriate
   section, not used to reorganize it. Unavailable sections explain why.

Completion: a reference inventory, capability matrix and shared navigation map;
a user can describe an action's location once for all clients.

#### 12.2 — Establish the Nexus design language

1. Extend `INTERFACE_PLAN.md` with design tokens, component anatomy and state
   examples before building full screens. Establish quiet neutral surfaces,
   readable primary text, restrained accent use, consistent spacing and clear
   hierarchy. Use colour plus labels/glyphs for status. Context notices,
   user text, agent text and execution diagnostics need distinguishable roles.
2. Define shared semantic tokens: canvas, panel, raised surface, primary and
   secondary text, border, accent, focus, selection, success, warning, error,
   additions and deletions. Supply light/dark themes and a monochrome terminal
   mapping. Define terminal cell spacing and NSH pixel/typography scales from
   the same hierarchy; do not mechanically translate every cell to pixels.
3. Specify header, session row, context section, turn header/footer, tool row,
   child card, composer, waiting card, settings row, dialog, banner and toast.
   Include default, focused, selected, hovered, pressed, disabled, loading,
   error and empty states. Align labels, counts, status marks and key hints.
4. Build a fixture-driven component gallery in the real renderers. Use the
   same sample session for cross-client comparisons. Review composition first:
   full workspace, starting context, streaming turn, tool expansion, child
   navigation, waiting input, settings and disconnected state. Then refine
   typography, padding, borders, truncation, colour and micro-interaction.
5. Capture light/dark at terminal 80×24, 120×36 and 160×48, NSH 390×844,
   1024×768 and 1440×900. Treat these as initial review sizes, not fixed
   breakpoints. Add short-height, text-scaling, Unicode and long-label cases.
   Keep a visible issue list and iterate until hierarchy and interaction feel
   consistent. Screenshot coverage alone is not visual acceptance.

Completion: a reviewed design language and representative real UI screens;
no collection of independently styled panels or unfinished placeholder states.

#### 12.3 — Build the TUI shell and API client

1. Scaffold an independent Rust project with pinned dependencies and documented
   build/run commands. Use Ratatui and a terminal input backend; separate API
   transport, event reduction, view state, layout, rendering and actions.
2. Implement Core discovery/auth, version handshake, durable SSE cursors,
   snapshot resync, keyed mutations and revision conflicts using Step 11's
   contracts. Clients never read SQLite, provider credentials or session files.
   Only the local bootstrap may discover/start Core through its documented
   mechanism. Distinguish starting, connecting, live, reconnecting, offline,
   authentication failure and incompatible API states.
3. Build the header, both sidebars, centre view, composer, palette, settings
   navigation and unified alerts. Keep focus explicit; overlays restore their
   trigger's focus and scroll position. Use common geometry for drawing and
   hit-testing. Resize without stale hit targets or losing the draft.
4. Establish mouse and keyboard parity; define shortcut precedence so text
   editing, dialogs, back navigation and interrupt do not compete for Esc.
   Handle terminal restoration on errors, suspend/resume, bracketed paste,
   Unicode display widths, ASCII glyph fallbacks, NO_COLOR and reduced motion.

Completion: a usable keyboard-first shell against a fixture daemon and real
Core, with reliable connection state, navigation and terminal cleanup.

#### 12.4 — Deliver the TUI's complete session flows

1. Implement project/session selection, worktree grouping, search, rename,
   archive/unarchive, export and confirmed deletion. Preserve the selected row,
   filters and position across updates. Clearly distinguish loading, empty,
   filtered-empty and failed states.
2. Implement draft starting-context inspection and configuration, then the
   ordered timeline: all model-visible context, streamed replies, reasoning,
   tool arguments/results, notices, child launches and handoff links. Keep wire
   and retry diagnostics explicitly separate. Folding never removes context.
3. Implement tool-specific expansion, nested `code` calls, kernel inspection,
   background job evidence, clipping labels and full-artifact retrieval. A UI
   preview limit must never be confused with a model-context limit.
4. Implement the multiline composer, paste and attachments, slash completion,
   steer/queue, explicit continue and handoff against their Core endpoints.
   Add the unified waiting card for root/child questions and approvals; expose
   only supported actions, with explanations for unavailable ones.
5. Add child breadcrumbs/back navigation, files/editor with stale-save
   protection, Git status/diff, settings, provider sign-in, skills/MCP controls,
   notifications and inspector/wire links. Retain the initial right sidebar's
   Session info and Git status scope; extra future sections require a spec change.
6. Verify scroll anchors while streaming, expanding tools and loading history.
   Follow only at the bottom; jump-to-latest restores following. Preserve
   positions per session and restore the parent at its child card.

Completion: run the agreed first-release journeys entirely in the TUI. Any
missing capability is explicitly deferred in the matrix, not represented by a
button that appears to work. Review and polish this reference before NSH parity.

#### 12.5 — Validate NSH and build web/desktop from the same interface

Read these upstream docs before choosing implementation details (reviewed
2026-10-27; recheck against the pinned version when building):
[getting started](https://nsh.dev/docs/getting-started/),
[Rust](https://nsh.dev/docs/rust/),
[design](https://nsh.dev/docs/design-guides/),
[web](https://nsh.dev/docs/web/),
[macOS](https://nsh.dev/docs/macos/),
[networking](https://nsh.dev/docs/networking/),
[components](https://nsh.dev/elements/) and
[testing](https://nsh.dev/docs/testing-and-trace/).

1. Pin a tested NSH release/revision and record platform requirements. Spike
   authenticated streaming SSE, cancellation, clipboard, input/IME, virtualized
   variable-height content, Markdown, editor/diff and accessibility in a browser
   and an actual desktop window. Choose Rust, TSX or a mixed app from evidence:
   current networking docs distinguish Rust whole-response fetch from TSX
   streaming responses. Prove a supported streaming path on both targets before
   committing to an all-Rust transport. Record gaps and supported fallbacks.
2. Build one NSH app for web and desktop, using NSH base components for focus,
   menus, dialogs, tabs and panels, styled with the Nexus tokens. NSH's web
   backend uses HTML/CSS; macOS uses native AppKit views. Keep platform adapters
   narrow: window chrome, native menus, clipboard, notifications, file pickers
   and keyboard conventions. Do not build a separate web information architecture.
3. Implement the TUI shell and journeys in the same order using the shared
   manifest and fixtures. Preserve sidebar-toggle locations, section order,
   waiting-card position, command names and child navigation. Native menus and
   browser shortcuts may provide additional entry points to the same commands.
4. Use graphical space for readable typography, inline images, richer metadata,
   hover detail, selectable text, resizable panels and wide split diffs. Keep
   summaries and primary actions in their familiar places. Additional details
   belong in existing expansions or inspection panels; they must not move a
   common action or introduce a competing settings taxonomy.
5. Resolve browser hosting and connection behavior explicitly: same-origin
   serving or documented Core origin policy, bearer-token acquisition/refresh,
   HTTPS/mixed-content restrictions, and authenticated SSE transport. A browser
   cannot read `daemon.json` or spawn the daemon. Offer the supported connection
   flow and actionable startup instructions; do not silently broaden Core's
   loopback binding or introduce a device-registration workflow.
6. Test the packaged web app and native desktop bundle against Core. Establish
   macOS first and list other desktop targets as verified or deferred; upstream
   platform support alone does not establish Nexus support. Keep iOS on the
   same navigation hierarchy, with drawers, safe areas and software-keyboard
   handling; decide remote Core access/deployment before claiming mobile ready.

Completion: browser and desktop run the same product with the TUI's navigation
and semantics, richer presentation, and documented platform capabilities.

#### 12.6 — Cross-client polish, parity and release checks

1. Run the same scenario fixtures across clients: first run/sign-in; starting
   context; normal and long streaming turns; nested tools; child questions;
   denial/cancellation; directory change; queued input; archive/export; stale
   file save; handoff; disconnect/restart/resync; expired history and conflicts.
   Compare command IDs, settings order, placement, context order and outcomes.
2. Add focused reducer/transport tests, Ratatui buffer and PTY interaction tests,
   and NSH headless interaction tests. Exercise real terminal/browser/native
   window input as well: automated snapshots do not establish IME, clipboard,
   selection, accessibility or visual polish. Reuse event fixtures, not Core
   business logic. Record exact commands with the chosen toolchain.
3. Check keyboard-only operation, visible focus, restoration, readable contrast,
   reduced motion, screen-reader labels on NSH, large text and narrow layouts.
   Resize during streaming, a dialog and a pending answer. Background activity
   must not steal focus or scroll position.
4. Measure a large timeline and session list on named hardware: startup,
   first usable frame, input latency during streaming, scroll smoothness, idle
   CPU and memory. Set budgets from the initial spike and track regressions.
   Virtualize/paginate without hiding context or losing selection/anchors.
5. Perform side-by-side visual review of TUI, web and desktop in light/dark,
   including errors and empty states. Close design issues through repeated
   real-app captures. Publish the capability matrix, packaging/run instructions,
   deliberate deferrals and verification evidence. Update this plan, the shared
   interface spec and current implementation docs as each increment lands.

Step 12 is complete when the agreed flows work across the supported clients,
navigation transfers directly between them, and the design and real interaction
checks pass. Feature presence alone does not satisfy the polish requirement.

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
| bash was synchronous with a fixed timeout | No default time limit (optional `timeout`), plus daemon-owned background jobs read with `bash_output`/`bash_kill` | A long command need not block a turn; its result arrives as a notice |
| Live events only; messages in the log | Every session event stored (`event` table) | The inspector can show any past session down to the wire |
| Agent prompts name their tools in prose | `{{tools}}` placeholder filled from the session's actual tools (`Tool.guidance`) | A switched-off tool is never described to the model |
| Fixed list of child agents in the orchestrator's prompt | The `session` guidance and enum are snapshotted from freshly checked agent files at each new session | New sessions pick up agent changes without rewriting existing prompts |
| Agent files read again on every turn | Resolved once into a stored `session_snapshot`; later turns replay it | A cached prefix stays stable; edits cannot silently change an existing session |
| No session instruction file | Exactly `<session.directory>/AGENTS.md`, labelled in the starting context with explicit absence/error/size status | Project instructions are visible to the model and inspector, without ancestor discovery |
| Old sessions have no original prompt record | Lazy reconstruction marked `reconstructed`, with a visible note and a one-time `session.snapshot` event | Honest migration; stable afterwards |
| Worktree tool, effectively mandatory | On-demand workspace controls: activate change-session-directory and worktree list/create/delete when needed (done 2026-10-27) | Keep ordinary sessions small and separate task work from Nexus state |
| A session always ran in its project path | A session can move to any existing directory; the project identity is unchanged and children inherit the move (done 2026-10-27) | Worktrees and sibling checkouts without pretending they are new projects |
| A directory change rewrote context | The move is announced forward, the destination `AGENTS.md` is loaded then, and earlier prompts/messages stay byte-for-byte (done 2026-10-27) | A cached prefix stays stable |
| Worktrees were created and removed by hand or without checks | Git argument arrays, real worktree metadata, dirty/main/in-use refusals and no branch deletion (done 2026-10-27) | Never destroy a checkout, a session's directory or an unrelated directory |
| Sessions had no archive state | `archived` on every session of a root tree; one atomic idempotent transition, default/only/include list filters, and notices that do not wake an archived tree (done 2026-10-27) | Hide finished work without deleting it, stopping jobs or losing context |
| Session history was inspectable but not portable | `GET /api/session/{id}/export` (Markdown transcript or versioned JSON with snapshots, messages and the retained event log), scoped to one session or its descendants, read from one SQLite read boundary (done 2026-10-27) | Take the context out without a provider call, a mutation or a silent truncation |
| The SSE `id:` was the per-daemon `seq`, so every restart invalidated clients | Durable event row ids with a `log` generation and a `<generation>:<id>` cursor; `seq` is only a live counter (done 2026-10-27) | A resume cursor must survive restart and reject a replaced database |
| Only session events were stored; project changes and deletions were live-only | Global lifecycle events and minimal `session.deleted` tombstones are stored and replayed; a session's own history still cascades (done 2026-10-27) | A reconnect must be able to rebuild project and session state |
| A reconnecting client started a fresh live stream and had to catch up out of band | Atomic replay-to-live handoff: subscription and head boundary first, paged replay, overlap skipped, explicit resync codes and overflow (done 2026-10-27) | No lost or duplicated events across the connection boundary |
| A crash left the last in-memory turn permanently busy, with no durable record of what ran | `turn`/`turn_request`/`turn_tool` records; startup recovery marks the turn interrupted, leaves the session idle and reconciles only durable evidence (done 2026-10-27) | Busy state was never durable, so recovery needs its own evidence |
| An interrupted provider stream could be mistaken for a finished reply | Partial output stays diagnostic in the event log; only a response that durably reached `Done` is restored, and a recovery notice labels the rest (done 2026-10-27) | Never present partial text, arguments or reasoning as complete |
| A crashed daemon silently dropped pending approvals and questions | Recovery publishes explicit `permission.replied`/`question.replied` cancellations; late ids cannot resume work (done 2026-10-27) | A vanished wait is not an answer |
| Continuing meant sending a new message ("continue"), which appended model-visible text | An explicit `POST …/continue`, `/continue` and `nexus session continue ID` send the stored context unchanged, reconcile dangling calls from durable evidence, and record continuation provenance (done 2026-10-27) | The model receives exactly the context the user sees, with no invented user turn |
| Compaction rewrites the session's history | A handoff to a new root session linked by `continued_from` (planned, Step 12a) | What the user sees stays what the model saw |
| System prompt rendered per turn | Rendered once at session creation and replayed unchanged | A stable cached prefix |
| Workspace edits require approval unless allowed | In-directory `edit`/`write` (patches and nested calls too) are auto-approved with no setting; bash approval is unchanged (done 2026-10-27) | Editing authorization and command execution are separate |
| Read before edit was prompt advice | Per-session observations persist hash, ranges and clipping; local changes need affected-line coverage and whole-file operations need complete same-revision coverage (done 2026-10-27) | Stale or unseen content cannot be changed through the file tools |
| No mutation serialization for files | One per-daemon lock, revision recheck before write, whole-patch validation before any write (done 2026-10-27) | Nexus writers cannot interleave a check and a write |
| Bash can write files with no tool checks | Documented enforcement boundary; full sandbox/restricted execution remains open (PLAN.md, Known gaps) | Do not claim a filesystem invariant the tool contract cannot guarantee |
| Web tools named `websearch` / `webfetch`, deployment in `websearch/` | `web_search` / `web_fetch`, deployment in `nexus-search/` (planned, Step 11a) | Explicit names and self-contained service setup |
| Extension system and MCP integrations in old Core | Focused skills/MCP support and upstream browser-use default in `nexus-browser/` (planned, Step 11a) | Carry over needed capabilities deliberately |
| A lost mutation response was ambiguous and a retry could create a second turn | `Idempotency-Key` mutation journal; prompt/continue acceptance and turn are one transaction; a replay returns the original result and schedules nothing (done 2026-10-27) | Clients can resolve a lost response without duplicating work |
| Competing updates silently overwrote each other | Monotonic project/session `revision`, `If-Match`/`expected_revision`, and 409 `revision_conflict` before any change (done 2026-10-27) | Conflicts are deterministic and visible |
| A transient provider failure ended the turn immediately | Bounded, visible pre-stream retries with attempt rows and events; no tool/partial replay (done 2026-10-27) | Survive transient faults without hiding attempts |
| A reconnecting client had to catch up out of band | `/api/snapshot` watermark plus `Client.watch` reconnect/deduplicate/resync; CLI cursor advances only after applying (done 2026-10-27) | Rebuilding state is consistent and cannot resubmit a prompt |
| The event log grew forever and the floor stayed 0 | Prefix retention with a durable head high-water mark, recovery/replay pins and an explicit `history_unavailable` boundary (done 2026-10-27) | Storage is bounded without losing recovery evidence |
| Separate TUI and NSH designs | One interface spec for both (INTERFACE_PLAN.md) | Same features, same names |

## Implementation sequence

Work one reviewed increment at a time:
1. Steps 8–9 and the requested Step 9a are reviewed and verified, with the
   reviewed diff prepared. Commit them as separate Conventional Commits and
   push `nexus10`.
2. Persist session snapshots and load session-directory `AGENTS.md` (Step 10
   foundation) — done 2026-10-27; see Step 10.
3. Add `ask`, approval-free workspace edits and read freshness checks; resolve
   the unrestricted-bash enforcement boundary before claiming full enforcement.
   `ask` and approval-free edits/read freshness are done; the bash boundary is
   documented, and full enforcement (sandbox or restricted commands) remains
   open.
4. Add on-demand workspace controls and directory/worktree operations — done
   2026-10-27; see Step 10 (`expose_workspace_tools`,
   `change_session_directory`, `worktree`, API and CLI).
4a. Add session-tree archive/unarchive, list filters, CLI commands and archived
   notification handling — done 2026-10-27; see Step 10.
4b. Add Markdown/JSON session export over the API, with an explicit read
   boundary and CLI file handling — done 2026-10-27; see Step 10.
5. Durable events and reconnect-safe replay — done 2026-10-27; see Step 11.
   Durable turn recovery — done 2026-10-27; see Step 11. Manual continuation
   (`/api/session/{id}/continue`, `/continue`, `nexus session continue`) — done
   2026-10-27; see Step 11. The ordered 11.1–11.7 increments — mutation request
   IDs and conflicts, bounded provider retries, client reconnect/resync,
   retention and integrated verification — are done 2026-10-27; see the
   delivery record below Step 11. No live provider or browser run was performed
   for this final increment.
6. Bring artifact storage forward from Step 12a before web/MCP image results.
7. Web tools, skills, MCP and remaining interface Core endpoints.

Each increment updates matching docs and this plan, runs focused tests plus the
required suite/lint, and supplies a concrete diff and verification for review.
Do not start the next increment until the current review is complete.

## Known gaps

- Steps 8–9a are delivered as one reviewed baseline; see the delivery review
  for independently rerun checks and the remaining platform limitations.
- Kernels have no checkpoint recovery; a timeout, interrupt, crash or resource
  limit discards the kernel. Startup recovery reports the loss for a reconciled
  turn and preserves nested evidence, but restores no variables or cells. Monty
  workers run inside the daemon with its environment (a scrubbed helper process
  is planned before 0.10.0). Linux, a forced worker crash and cold-start latency
  are unverified.
- Background bash jobs live in the daemon's memory. A normal shutdown kills
  them; a daemon crash can leave the command running with no job record, and
  retained output is lost. Recovery reports an unfinished `bash.started` when it
  reconciles that session's turn, but a job that outlives an orderly turn in a
  session with no interrupted turn is not reported individually. Recovery never
  kills a process from a stored pid (it may have been reused); there is no job
  supervisor.
- Approval cancellation now publishes `permission.replied` with an explicit
  outcome during normal teardown, cell cancellation and startup recovery.
  Questions and approvals remain distinct waits; none survives a restart.
- Startup recovery closes an interrupted outer `code` call with an unknown
  outcome, preserving the durable `code.call` and `code.cell` events and saying
  the kernel was lost. It does not invent a successful call or recover kernel
  state.
- A provider retry can duplicate provider-side computation/billing; it is
  bounded and visible, not provider-side exactly-once. An ambiguous external Git
  effect (worktree create/delete) is persisted only as intent/evidence and must
  be inspected, never blindly repeated. Manual continuation is an explicit
  client action and never repeats an ambiguous POST. There is no context limit
  or compaction (planned as handoff, Step 12a).
- Explicit updates apply to subsequent model/tool calls. An active `code` cell
  retains the task-tool bindings it started with until that outer call finishes.
  Agent switches retain the initially loaded `AGENTS.md` section; a directory
  change loads the destination's instructions as forward context instead of
  rewriting the frozen prompt.
- A client-requested directory move that is waiting for a running call lives in
  memory: a daemon crash before the next boundary loses it. Worktree deletion
  checks session directories and pending moves, not arbitrary `bash` or external
  processes using the worktree. There is no merge/integrate or branch-cleanup
  operation and no worktree permission setting.
- Archive transitions are stored in the root session's event log only; a client
  subscribed solely to a descendant stream does not see the root's transition.
  Archive has no bulk/project operation, timestamp or worktree cleanup (by
  design archiving never deletes one), and background jobs keep running.
- A session export holds one SQLite read snapshot (WAL) while streaming, so a
  very large export delays checkpointing until it finishes; writers are not
  blocked. Markdown omits wire events, and the JSON document (with every event)
  can be much larger than the messages alone. Export has no attachments or
  artifacts to include yet and does not re-clip output.
- Sessions from before snapshots existed are reconstructed once and marked
  `reconstructed`; the note is visible, but the original prompt is unknowable.
- Reconnect resumes the event stream; startup recovery reconciles unfinished
  turns before the daemon serves requests and publishes their idle status,
  cancellations and `turn.recovered`. Manual continuation after recovery is an
  explicit request. Keyed mutations replay safely and clients resync from
  `/api/snapshot` on a cursor failure or overflow. Archive transitions are still
  stored only in the root's log.
- Event, turn lifecycle and mutation retention is bounded: a contiguous event
  prefix expires by age/payload, while unfinished turns, open waits, live jobs
  and in-flight replays pin their evidence. Messages, snapshots and model context
  are independent of diagnostic expiry. A cursor below the floor gets
  `history_unavailable` with a resync head. Expired keys are no longer safe to
  retry automatically. A very large export still holds one WAL read snapshot
  while streaming.
- Sign-in is only the local `nexus login`, not yet an API endpoint.
- Nothing checks that a running daemon runs the current code: `nexus daemon stop` after changes.
- No cross-provider message conversion beyond text and tool calls; switching
  provider mid-session works (`/model`), but foreign reasoning is dropped.
- Copilot GPT models are sent over Chat Completions; whether some of them
  require Responses is not checked.
- No images in either dialect yet.
- Tool paths are confined to the workspace, but `bash` is not sandboxed, so
  "read-only" (`scout`) and "Markdown only" (`plan`) rest on their prompts.
  Read-before-edit is enforced only for the `edit`/`write` tools: unrestricted
  bash can write any file without an observation. There is also no in-process
  control over external writers, so a file can change between the final hash
  check and the write. Full enforcement needs a sandbox or restricted
  write-capable command execution before the invariant can be guaranteed.
- File observations persist per session and have no API view or pruning yet;
  they are removed with their session and replaced when a newer revision is
  read. An interrupted read that never produced a tool result is dropped.
- Child sessions launched with `wait: false` are tested offline only.
- `instructions` uses the Responses field. The current Codex CLI sends a
  developer message instead. Both work today; revisit if the backend changes.


Delivery review of durable event replay: preserved the old AUTOINCREMENT high
watermark even when its highest rows were deleted; included migration DDL in an
explicit transaction; rejected explicitly empty cursors instead of falling back
to live/header behavior. Inspector replay now deduplicates HTTP-loaded events
and messages independently, so a later event-page boundary cannot hide a message
missing from an earlier message fetch. Added regression coverage including a
Node-driven inspector load/replay race. Full reviewed suite: 244 tests.


Startup recovery delivery review: closed turns remain pending until final
cleanup, covering crashes between the closing record and idle publication;
normal idle events carry turn ids for deduplication. Approval replies persist
before releasing a tool. Message-link migration DDL and legacy markers are
transactional. Added terminal-cleanup crash, approval-storage failure, and
migration rollback regressions. Reviewed full suite: 279 tests.


Manual continuation delivery review: linked tool lifecycle evidence to the saved
assistant message, avoiding reuse of old outputs or approval decisions when a
provider reuses call ids. Added migration of that nullable link, conservative
fallback through stored request links, and fresh execution state for reused ids
within a turn. Added three regressions for repeated ids and stale approval
classification. Also ignore a prior turn's idle event queued before the continuation starts.
Reviewed suite: 300 tests; no live provider or browser run.
