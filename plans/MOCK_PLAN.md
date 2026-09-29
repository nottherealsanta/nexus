# MOCK_PLAN — scripted mock scenarios in dev mode

Status: implemented (phases 0-4 and the scenario set of 5-8; see 'As built'). Owner: TBD. Cites: plan section 8 (ScriptedProvider), 14.11
(commands as data), 15.x (subagents), STATE_PLAN §5 (home layout).

## 1. Goal

Launch Nexus in **dev mode** and type `/mock` to run a named, scripted scenario
end to end through the real daemon, loop, tool dispatcher, subagent runner,
reducer and both UIs (Textual and web). There are two hard rules:

1. **No real LLM call.** Every model request in a mock session is answered by a
   deterministic mock provider. If a request would reach a real provider, the
   request fails closed.
2. **No effect on the user's system.** Tools really execute, but only inside a
   throwaway sandbox workspace. Anything that cannot be sandboxed (network,
   very long waits) is simulated.

The point is to exercise every surface of Nexus (long tool chains, parallel
tools, parallel and nested subagents, approvals, questions, cancellation,
errors, compaction, reconnect and replay) on demand and repeatably, both by hand
and in CI.

## 2. Decision: real tools in a sandbox, simulated where they must be

The request allowed two options: fake everything, or run real tools inside a
folder they cannot escape. This plan recommends the **sandbox as the default**
and adds **per-step simulation** as an escape hatch:

| Option | Tests real code paths | Risk | Verdict |
| --- | --- | --- | --- |
| Fully fake tools (dispatcher returns canned results) | UI and reducer only. Skips permissions, path guard, diffs, worktrees, job control, truncation | none | Too shallow to be the default |
| Real tools in a sandbox workspace | Everything below the provider | Contained by PathGuard, the sandbox root, allowlisted scripted commands and network stubs | **Default** |
| Per-step `simulate=` (canned result, optional delay, no execution) | UI shape for things that cannot run safely or quickly | none | Use for WebFetch/WebSearch, 10-minute Bash, huge outputs |

Scripted tool calls are written by us, not produced by a model. The only
commands that can run are the ones in the scenario files, and the path guard
still bounds them.

## 3. User-facing behavior

### 3.1 Dev mode

- Turn it on with `nexus chat --dev`, `nexus web --dev`, `nexus run --dev …`, or
  `NEXUS_DEV=1`. It is a top-level flag, parsed in `nexus/cli.py` next to
  `--workspace`.
- Dev mode uses an **isolated home**, `~/.nexus/dev/`, via the existing
  `NEXUS_HOME` seam. That gives it its own `nexus.db`, daemon socket, lock and
  logs, so mock sessions never show up in the user's real session list, and the
  dev daemon never collides with the normal daemon for the same workspace.
  `--dev-home PATH` overrides the location.
- The daemon starts with `--dev`, and `Health`/status reports `dev: true`.
- Both UIs show a `DEV` badge in the top-bar status area.
- Real providers still work in dev mode if the user configures them in the dev
  home. Mock sessions never use them (see §5.4).

### 3.2 The `/mock` command

`CommandSpec("/mock", "Run a scripted mock scenario", "[list|NAME|clean] [--speed N] [--seed N]", dev_only=True)`

- `CommandSpec` gets a new `dev_only: bool = False` field. Help, completion and
  dispatch hide dev-only commands unless the connected daemon reports
  `dev: true`. Outside dev mode, `/mock` returns "unknown command", the same as
  any other unknown command.
- `/mock` or `/mock list` opens a picker with each scenario's name, one-line
  summary, tags and rough duration. It reuses the model-picker list widget in the
  TUI and the same dialog pattern in web.
- `/mock NAME` creates a **new session** titled `mock: NAME`, switches to it,
  and starts the scenario's kickoff turn right away.
- `--speed` scales streaming and tool delays (`0` means instant, which CI uses).
  `--seed` fixes jitter so timing-sensitive runs reproduce.
- `/mock clean` deletes old sandboxes. At most 20 are kept, oldest removed first.
- `nexus mock list` and `nexus mock run NAME [--json] [--speed 0]` provide the
  same thing headless, for CI and for quick terminal checks.

### 3.3 End-of-run verdict

Each scenario declares **expectations** (§6.3). When the kickoff turn and all
its children settle, the daemon checks the reduced view against those
expectations and appends a `mock.verdict` record. It renders as a closing
notice, for example `✓ parallel-subagents: 14/14 checks passed`, or a list of
the checks that failed. This is how "see if it all works" gets answered without
reading the whole timeline.

## 4. Architecture

```
nexus/devtools/                 # new package; manager layer (sits beside agents/skills)
  __init__.py
  mock/
    __init__.py                 # public: MockProvider, Scenario, load_catalog
    provider.py                 # MockProvider: stateless, request-keyed replay
    dsl.py                      # Scenario/Actor/Step builders (text, think, tools, task, wait, fail…)
    directive.py                # ⟦mock …⟧ actor directive parse/format
    sandbox.py                  # create/seed/retain/clean sandboxes; containment checks
    stubs.py                    # outbound-HTTP + search stub transports, MCP fixture server path
    verdict.py                  # expectations → checks over the reduced view
    scenarios/                  # one module per scenario (data-first, Python for branching)
      hello.py tool_marathon.py parallel_tools.py parallel_subagents.py …
    seed/                       # sandbox seed project (small fake repo, skill, hook, mcp server)
```

- **Layering.** `nexus.devtools` imports `model/`, `tools/spec`, `view/` and
  `events`. It never imports `host/` or `ui/`. `runtime.py` and `host/` import
  it only when dev mode is on (a lazy import in one place each).
  `tests/test_layering.py` gets the new package at the manager level.
- **Budgets.** `devtools/` sits outside the core/model/host/ui budgets. The only
  counted additions are thin: the dispatch in `host/facade.py` (about 60 lines;
  host is close to its cap, so the logic lives in `devtools` or
  `host_support/mock.py`), plus `/mock` handling in the TUI panel module and the
  CLI. Nothing goes in `ui/tui/app.py` beyond a single dispatch line.

### 4.1 Host contract (`nexus/host/protocol.py`)

| Command | Result | Notes |
| --- | --- | --- |
| `MockList()` | `MockListResult(scenarios: list[MockScenarioInfo])` | name, summary, tags, est_seconds, needs (approvals, question, git) |
| `MockStart(scenario, speed=1.0, seed=0)` | `MockStartResult(session, sandbox, scenario)` | creates the sandbox and the session, then starts the kickoff turn |
| `MockClean(keep=0)` | `MockCleanResult(removed)` | bounded |

When the daemon is not in dev mode, the facade answers every `Mock*` command
with a redacted `ErrorResult("mock scenarios require dev mode")`. All three
clients (TUI, web, CLI) call only these commands, following rule 4.

### 4.2 Durable records (rule 5)

- `mock.started {scenario, version, speed, seed, sandbox}` is the first record
  of a mock session. The reducer exposes it as `view.mock`, which drives the
  `mock:` title prefix and the badge. Replay rebuilds it.
- `mock.verdict {passed, checks:[{name, ok, detail}]}` is appended by the
  daemon, never by a client.
- Everything else is the normal records (turns, tool calls, `agent.*`), because
  the real loop produces them. That makes replay parity a direct check.

## 5. The mock provider

### 5.1 Stateless, request-keyed replay

`ScriptedProvider` consumes scripts in call order. That breaks once parallel
children share one provider, and after reconnect, fork or restart.
`MockProvider` instead **derives its position from the request itself**:

1. **Actor.** Look for a `⟦mock scenario=S actor=A⟧` directive in the first
   user message of `req.messages`. The root kickoff prompt carries
   `actor=main`. Every scripted `Task` call embeds the directive for its child
   in the prompt it passes. With no directive, the provider raises
   `MockRouteViolation`, so a request that did not come from a scenario is never
   answered by guesswork.
2. **Step.** The step index is the number of assistant messages already in
   `req.messages`. Step 0 is the first reply, step 1 comes after the first tool
   results, and so on.
3. **Branching.** A step can be a callable `(req, ctx) -> events`. `ctx` exposes
   the last tool results (including status: ok, error, denied, cancelled), the
   last user text and the speed/seed settings. That lets scenarios take
   different paths on "permission denied" or on the user's answer to a
   `Question`.

The result is concurrency-safe (no shared cursor) and resumable: a forked or
replayed session continues from where its messages leave off. It also matches
how a real model behaves, since each reply is a function of the conversation.

### 5.2 Events and timing

Steps compile to the existing normalized `StreamEvent`s (`MessageStart`,
`TextDelta`, `ThinkingDelta`/`ThinkingEnd`, `ToolCallStart`/`ToolCallDelta`/
`ToolCallEnd`, `Usage`, `MessageStop`). They reuse `text_response`/
`tool_response` from `scripted.py` where they fit. Text streams in chunks of
roughly 3 to 12 characters with a per-chunk delay scaled by `speed`, so the UI
really exercises streaming. Tool arguments stream as `ToolCallDelta` fragments.
`Usage` values are scripted, which is how `/cost` and context-pressure paths get
tested.

### 5.3 Capabilities and models

The provider is `name="mock"`, with one model per scenario (`mock/<scenario>`).
Capabilities are all on (tools, parallel_tool_calls, streaming, thinking), and
per-model `max_context_tokens` is small for the context-pressure scenario. In
dev mode the provider is registered in the runtime's provider map and its models
in the registry as selectable, tier `low`, with explicit reasoning-effort
levels, so `/model` and `/effort` show them and the runner's tier clamp accepts
them.

### 5.4 Fail-closed routing

A mock session tree is a root session with a `mock.started` record, plus every
child whose logical id is `<root>/sub/…`. Every request in that tree must resolve
to `MockProvider`. The dev runtime wraps `provider_for` for these sessions. If
the router resolves to anything else (a tier that maps to a real model, a role
with a hard-coded provider, a fallback), it raises `MockRouteViolation`. The
turn then fails visibly and the verdict records it.

Scripted `Task` calls also pass `model="mock/<scenario>"` explicitly, so the
normal path never depends on the guard. The guard is the backstop.

## 6. Scenarios

### 6.1 DSL sketch

```python
SCENARIO = Scenario(
    name="parallel-subagents",
    summary="Root fans out 5 Task calls (cap 4) with mixed outcomes and a grandchild",
    tags=("agents", "parallel"),
    actors={
        "main": [
            say("Splitting the audit across five workers."),
            tasks(*(task(f"worker-{i}", type="task", prompt=f"Audit area {i}") for i in range(1, 6))),
            say_summary_of_children(),          # callable step reading tool results
        ],
        "worker-1": [tools(read("src/app.py"), grep("TODO")), say("Found 3 TODOs.")],
        "worker-3": [think(800), fail(ProviderError("scripted 529 overloaded"))],
        "worker-4": [task("grand-1", prompt="Deep dive"), say("Grandchild done.")],
        "grand-1":  [tools(ls("src")), say("Leaf report.")],
        ...
    },
    expect=[
        tool_calls(actor="main", name="Task", count=5),
        children(total=6, max_concurrent_observed__le=4),
        child_status("worker-3", "error"),
        final_phase("idle"),
        sandbox_only(),                          # nothing outside the sandbox changed
        replay_matches_live(),
    ],
)
```

The helpers are `say`, `think`, `tools(...)` (one message holding parallel
calls), `tool(...)`, `task(...)`, `wait(seconds|until="cancel")`, `fail(exc)`,
`malformed(...)`, `usage(...)` and `simulate(result, delay)`. There is one
helper per builtin tool: `read`, `write`, `edit`, `multiedit`, `apply_patch`,
`bash`, `bash_output`, `kill_shell`, `ls`, `glob`, `grep`, `todo`, `question`,
`skill`, `webfetch`, `websearch` and `mcp(server, tool, args)`.

### 6.2 Catalog (first set)

| Name | What it exercises |
| --- | --- |
| `hello` | Smallest possible run: one streamed markdown reply. Smoke test for dev mode. |
| `tool-marathon` | About 60 sequential steps using **every builtin tool** at least once: read/write/edit/multiedit/apply_patch on the seed repo, Bash foreground plus a background job with BashOutput and KillShell, Glob/Grep/Ls, TodoWrite updates, Skill activation. Tests timeline length, collapsing, `/tools`, `/verbose` and scroll performance. |
| `parallel-tools` | A single assistant message with 8 parallel read-only calls, then a mix of serial and parallel writes. Checks ordering, result slots and concurrency flags. |
| `parallel-subagents` | 5 `Task` calls at once against `max_concurrent=4`, so queueing happens. The workers have different durations; one fails, one spawns a grandchild. Tests the tasks panel, `/tasks`, agent transcripts and the details sidebar. |
| `agent-limits` | Hits max depth (3) and max fanout (16), plus a tier clamp. Checks that `agent.clamped` appears and that the limit errors reach the model. |
| `worktree-agents` | Two `Task` calls with worktree isolation in the sandbox git repo, each making conflicting edits. Tests `/worktrees` review, integrate and discard. |
| `approvals` | Calls that need permission: approve once, deny (the scenario takes the denied branch), "always" grant, then an unattended child's denial. |
| `question` | The `Question` tool with choices. The scenario branches on the answer, and a second question gets cancelled. |
| `streaming-rich` | A long reply with headings, nested lists, tables, many code blocks, 400-column lines, CJK/emoji/RTL text, and escape-looking text that must render literally. Includes thinking deltas. |
| `errors` | Provider error mid-stream, then retry. Malformed tool JSON, a duplicate tool-call id, an unknown tool, a tool that raises, a tool timeout, `max_tokens` and `refusal` stop reasons. |
| `cancel` | Parks on `wait(until="cancel")` during streaming, during a long `simulate`d Bash, and during parallel subagents. The user runs `/cancel`. The verdict checks that everything unwound. |
| `queue` | A long turn while the user enqueues messages. Later steps echo the queued inputs so ordering can be verified. |
| `context-pressure` | Large tool outputs past `max_result_tokens`, and scripted `Usage` that climbs toward a small context window to trigger compaction and summary reuse. Checks `/context` and `/cost`. |
| `extensions` | Activates a sandbox skill, calls the sandbox stdio MCP fixture server, and fires a sandbox hook. `/mcp`, `/skills` and `/reload` run mid-scenario. |
| `diff-review` | Edits in the sandbox repo, then `/diff` and `/review` / `/commit` prompts, which continue as scripted actors. |
| `reconnect` | A slow 60-second turn. The tester runs `/reconnect` or reloads the browser tab mid-turn. The verdict checks replay parity. |
| `stress` | 500 tool calls, 20 subagents and around 50k events, for performance and memory in both UIs. Excluded from the default CI run. |
| `all` | Runs every non-interactive scenario in sequence, each in its own session, and prints a summary table. |

Interactive scenarios (approvals, question, cancel, queue, reconnect) have an
`auto` variant for CI. In that variant the headless runner answers approvals and
questions and sends `/cancel` through the host contract at scripted points.

### 6.3 Expectations and invariants

Every scenario gets these checks automatically, in addition to the ones it
declares:

- The session ends in phase `idle`, with no open tool calls, pending approvals
  or running children.
- Every request in the tree was answered by `MockProvider` (a count of
  violations, which must be 0).
- **Sandbox containment.** A content hash of the real workspace, and of
  `~/.nexus` outside the dev home, is the same before and after. Every file
  change is under the sandbox.
- **Replay parity.** Reducing the durable records from scratch gives the same
  view as the live view.
- No uncaught exceptions in the daemon log for this session.

## 7. Sandbox

- **Location.** `<dev home>/mock/<yyyymmdd-hhmmss>-<scenario>/workspace/`.
  Bounded retention: 20 runs and 200 MB. `/mock clean` clears it.
- **Seed.** `devtools/mock/seed/` is copied in and turned into a git repo with
  one commit: a tiny Python project, a README, a TODO-laden file, an
  `.agents/skills/mock-skill/`, an `.agents/hooks` entry, and an MCP stdio fixture
  server script configured in `.agents/`.
- **Scoping.** A mock root session runs its tools with the sandbox as its
  workspace. Phase 0 checks the best seam for this. The preferred seam is a
  per-session workspace override that reuses the machinery children already use
  (`_child_path_guard`, `_child_workspace_config`, `_build_child_tool_manager`
  with `workspace=`), persisted in `mock.started` so it survives reconnect.
  PathGuard `write_roots` is the sandbox only, and read-deny covers the real
  workspace.
- **Bash.** Commands come only from scenario files. They run with cwd set to the
  sandbox, `HOME=<sandbox>/home` and a minimal `PATH`. A unit test checks that
  every scenario's Bash strings match a small allowlist (`ls`, `cat`, `echo`,
  `python -c`, `git`, `sleep`, `grep`, `wc`) and contain no absolute paths or
  `..` outside the sandbox.
- **Network.** In mock sessions, the `outbound_http` and `local_search_http`
  services are replaced with stub transports that serve canned pages and search
  results from `seed/web/`. Any request to an unknown URL returns an error. No
  sockets are opened.
- **Credentials.** The dev home holds none by default, and mock sessions never
  need any.

## 8. UI work (the two surfaces mirror each other)

| Piece | Textual (`ui/tui/`, `ui_support/`) | Web (`ui/web/`) |
| --- | --- | --- |
| `DEV` badge | top-bar status | top-bar status |
| `/mock` picker | list modal (reuse the model-picker list) in a new `ui/tui/mock.py` | dialog in `js/mock.js` |
| Mock session marker | `mock:` title prefix plus a scenario chip in the context header | same |
| Verdict notice | closing timeline notice, with a pass/fail color | same |
| Help and completion | hide `dev_only` unless dev | same |

The CLI gets `nexus mock list|run` in `ui/cli/` with a JSONL mode, via
`ui/jsonl.py`.

## 9. Testing

- **Unit.**
  - Directive parse and format.
  - Step indexing from message history, including fork and replay.
  - DSL compile to `StreamEvent`s.
  - Fail-closed routing.
  - Sandbox create/retain/clean bounds.
  - Bash allowlist lint over the whole catalog.
  - `dev_only` command gating.
- **Scenario matrix.** `tests/test_mock_scenarios.py` is parametrized over the
  catalog, with `speed=0` and `auto` variants. Each scenario runs through a real
  `HostFacade` and `Runtime` in a tmp home, and the test asserts `mock.verdict`
  passed. This becomes the broad regression net for the loop, runner and
  reducer. `stress` gets a `slow` mark.
- **Protocol.** `MockList`, `MockStart` and `MockClean` are tested in
  `tests/test_host_facade.py`, including the "requires dev mode" error.
- **Browser and Textual.** `tests/playwright_web_check.py` and
  `tests/playwright_tui_check.py` gain a dev-mode section: open the `/mock`
  picker, run `parallel-subagents` and `streaming-rich` at `speed=0`, wait for
  the verdict, and save screenshots to `artifacts/`.
- **Layering and budgets.** The existing tests cover them once `devtools` is
  registered.

## 10. Phases

| # | Deliverable | Exit check |
| --- | --- | --- |
| 0 | **Spike.** Choose the root-session workspace override seam. Confirm that `mock/*` refs pass the runner's tier clamp and registry lookup. Confirm stub injection points for outbound HTTP and search. | A short note appended to this plan; no product code |
| 1 | Dev mode: `--dev`/`NEXUS_DEV`, isolated home, daemon `--dev`, `Health.dev`, `DEV` badge, `CommandSpec.dev_only` | The normal and dev daemons run side by side for one workspace |
| 2 | `MockProvider` plus DSL plus directive plus fail-closed routing; scenarios `hello`, `parallel-tools`; `nexus mock run` | Headless runs pass; violation test passes |
| 3 | Sandbox (seed, scoping, Bash env, network stubs, retention) plus the containment invariant; `tool-marathon` | Workspace hash is unchanged across the full marathon |
| 4 | Protocol `Mock*`, `mock.started`/`mock.verdict` records, reducer `view.mock`, `/mock` in TUI and web, verdict notice | Playwright and Textual checks are green |
| 5 | Agent scenarios: `parallel-subagents`, `agent-limits`, `worktree-agents` | Verdicts pass; transcripts render in both UIs |
| 6 | Interactive: `approvals`, `question`, `cancel`, `queue`, `reconnect`, plus `auto` variants | CI matrix passes with no human input |
| 7 | Failure and pressure: `errors`, `context-pressure`, `streaming-rich`, `extensions`, `diff-review` | Verdicts pass |
| 8 | `stress`, `all`, docs (`docs/core.md`, `docs/textual.md`, `docs/web.md`, README "Dev mode") | Stress runs within a stated time and memory bound |

## 11. Risks and open questions

- **Root-session workspace override.** This is the largest runtime change.
  Phase 0 may find it cleaner for `/mock` to start a *second dev daemon* rooted
  at the sandbox, with the client switching to it (the web client opens a new
  tab; the TUI reconnects). The fallback is the scripted-path discipline
  (all paths relative to a `mock/<run>/` subfolder, plus a narrowed PathGuard
  `write_roots`), which is weaker. The plan keeps the in-daemon override as the
  goal.
- **Tier routing for children.** A role with a hard-coded provider or model
  could bypass `Task(model=…)`. The fail-closed guard catches this, but
  scenarios should pin `model` on every `Task` call.
- **Timing flakiness** in parallel scenarios. Use `speed=0` plus `seed` in CI.
  Verdicts check counts and final states, never interleaving order, except where
  a scenario is specifically about order (`queue`).
- **Scenario drift.** When tool schemas change, scenarios break. The matrix test
  is the early warning. Each scenario carries a `version`, which is recorded in
  `mock.started`.
- **Host line budget.** Keep the facade handlers to thin delegation into
  `devtools`/`host_support`, and don't move unrelated code to make room.

## 12. As built (deviations from the plan above)

- **Sandbox seam (phase 0 outcome).** No per-session workspace override. The
  CLI swaps the *whole workspace* for the sandbox in dev mode
  (`cli._enter_dev_mode`), so the real runtime, PathGuard and tools operate
  inside it. `MockClean` restores it in place from the seed commit.
- **Verdict is in-band.** The last scripted step writes `✓ mock verdict — …` as
  assistant text (a text-only message ends the turn, so it carries the closing
  words). There is no `mock.started`/`mock.verdict` record and no reducer
  change. Containment (sandbox/outside hash) is asserted in tests, not in-band.
- **Fail-closed routing** is provider-side (`MockRouteViolation` without a
  directive); there is no router wrapper.
- **Gating.** `/mock` is added to `commands.SPECS` at import when `NEXUS_DEV` is
  set (not a `dev_only` field). Setup is never required in dev mode.
- **Default tool set** has no `ls`/`multiedit`/`websearch`; scenarios use `read`
  on directories and `edit`. Web tools are never called (no network stubs
  needed).
- **Catalog built:** hello, tool-marathon, parallel-tools, parallel-subagents,
  agent-limits, streaming-rich, errors, provider-failure, cancel, question,
  context-pressure, diff-review, stress. Not built: worktree-agents, approvals,
  queue, extensions, reconnect.
- **TUI:** `_switch_session` only snapshots, so `/mock` follows the live run via
  `controller.resume` (as `/reconnect` does).
