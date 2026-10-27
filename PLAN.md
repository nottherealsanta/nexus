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
   write, and code (planned) to script them. Changing Nexus itself (which session, workspace or worktree, which
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

## Layout

Each part of the repo is a self-contained project with its own toolchain. The
root holds only plans and docs.

```
docs/             how the code works; start at docs/README.md
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
    tools/          read, bash, edit (replacement or apply_patch), write
    kernels.py      (planned) Monty kernels for the `code` tool, owned by the daemon
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

### Step 8: the `code` tool, Monty kernels (in progress)

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

**Planned design**
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

Not verified yet: Linux, worker crash recovery, memory-limit behavior,
cold-start latency, and many kernels at once.

**Not planned for version 1:**
- Kernel snapshots and restore after a daemon restart (old Nexus had signed
  checkpoints).
- Typed tool stubs and host-side argument checking.
- MCP tools inside the kernel.
- The `code_start`/`code_attach` handshake.

### Step 9: control plane

The rest of the `nexus` namespace: workspace, worktree, fork, move. Commands
with their own events, exposed as API endpoints. Model and agent switching
already exist (`POST /api/session/{id}/model`, `/agent`).

### Step 10: event log follow-ups

Retention and size limits for the `event` table, resuming the live stream from
an event id, and showing a crashed turn's partial state.

### Step 11: clients

Ratatui TUI (`nexus-tui/`) first, then NSH (`nexus-nsh/`), both on the HTTP
API. Both show the wire view, the session tree and kernels.

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
| Four kernel tools (`code_start`, `code_attach`, `code_run`, `code_reset`), one kernel per agent | One `code` tool; kernels owned by the daemon, shared by id across sessions (planned) | Simpler to use; a session tree works in one kernel |
| Live events only; messages in the log | Every session event stored (`event` table) | The inspector can show any past session down to the wire |

## Known gaps

- No retries, no context limit or compaction.
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
