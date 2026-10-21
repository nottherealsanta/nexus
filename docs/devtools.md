# Dev tooling: dev mode, mock scenarios, benchmark, examples

Nothing here is imported by the normal product path. `nexus.runtime` and
`nexus.host` import `nexus.devtools.mock` lazily, and only in dev mode.

## Dev mode

`nexus --dev <chat|web|mock …>` or `NEXUS_DEV=1` (truthy: `1`, `true`, `yes`,
`on`) swaps the workspace for a seeded, git-initialised **sandbox** under an
isolated home (`~/.nexus/dev`) and registers `MockProvider` (`mock/<scenario>`
models). Real tools run for real inside the sandbox (the `PathGuard` confines
them); nothing touches the network or your workspace. Setup is never required.
`HealthResult.dev` tells clients to show `/mock` and the `DEV` badge.

| Piece | File |
| --- | --- |
| `dev_enabled`, `DEV_ENV` | `devtools/__init__.py` |
| Scenario DSL: `Scenario`, `Turn`, `Fail`, `Hang`, `Dyn`, `Call`, helpers `say`, `calls`, `task`, `verdict`, `dyn`, `fail`, `hang` | `devtools/mock/dsl.py` |
| Stateless provider | `devtools/mock/provider.py` |
| `⟦mock …⟧` actor directive | `devtools/mock/directive.py` |
| Headless runner and verdict checks | `devtools/mock/runner.py`, `checks.py` |
| Sandbox (`SEED_FILES`, `GLOBAL_SEED_FILES`, `generated_seed_files`, `ensure_sandbox`, `reset_sandbox`, `tree_hash`) | `devtools/mock/sandbox.py` |
| Dummy MCP server (`tracker`, `docs`, `broken`) | `devtools/mock/mcp_server.py` |
| Debug manager MCP (the manager's session tools over stdio; [manager.md](manager.md#debug-mcp-server-debug-only)) | `devtools/manager_debug_mcp.py`, `debug_mcp_port.py` |
| Catalogue | `devtools/mock/scenarios/__init__.py` (`_MODULES`) |
| Host dispatch | `host_support/mock.py`; commands `MockList`, `MockStart`, `MockClean` (error outside dev mode) |
| Surfaces | `ui_support/mock_args.py`, `mock_cli.py`, `ui/ratatui/actions.py`, `ui/web/js/mock.js` |

**How the provider works:** it is stateless. The actor comes from a `⟦mock …⟧`
directive in the first user message and the step from the number of assistant
messages already in the request, so parallel subagents, forks and replays all
work. A request without a directive fails closed. Every scenario ends with an
in-band `✓ mock verdict — …` message. The sandbox seeds `nexus.toml`
(`mock/hello`, offline, `mode = "allow"`, `bash_yield_s = 3`), a README and a
small `src/app.py`. It also seeds extensions so the context header has real
data: project skills (`mock-skill`, `code-review` with `references/` files,
`release-notes`, `sql-style` with a very long description), global skills in the dev
home (`writing-style`, `git-hygiene`) and dummy MCP servers from
`devtools/mock/mcp_server.py`: `mock-tracker` (project, six rich tools, resources,
a prompt, instructions; `tool_loading: all`), `mock-docs` (global, 24 `lookup_*`
tools, loaded via search) and `mock-broken` (project, exits 3 before `initialize`,
so it shows `failed`). `--profile` selects the identity. The `mcp.json` files are
**generated** (they hold `sys.executable`), rewritten when they differ, and
git-ignored in the sandbox. **Seeding rule:** `ensure_sandbox` writes any *missing*
seed file on every start and never overwrites existing ones, so new seeds reach an
existing sandbox; global seeds are written only inside the dev home. The
`extensions` scenario does a real stdio MCP call, a deferred `McpSearch`/`McpCall`
and then loads `code-review` (last, because its `allowed-tools` narrows later calls).

**Scenarios** (`nexus mock list`): `hello`, `tool_marathon`, `parallel_tools`,
`parallel_subagents`, `agent_limits`, `streaming_rich`, `errors`,
`provider_failure`, `cancel`, `question`, `context_pressure`, `diff_review`,
`stress`, `bash_wait`, `extensions`. `parallel_subagents` and `agent_limits` pass `wait: true` to every `subagent` call, because they assert blocking results in the same turn (subagents are non-blocking by default; reports would otherwise arrive as later messages). Interactive ones (`cancel`, `provider_failure`, `question`)
need a person; `stress` is slow.

**Use:**

```sh
nexus --dev chat            # then /mock, /mock NAME [--speed N] [--seed N], /mock clean
nexus --dev web
nexus mock list | run NAME | run all | clean [--speed N --seed N --json]
```

`/mock NAME` starts the scenario in a new `mock-NAME-N` session and follows it live.

**Add a scenario:** write `devtools/mock/scenarios/<name>.py` exporting `SCENARIO`,
list it in `_MODULES`; `tests/test_mock_scenarios.py` runs every non-slow scenario
end to end. Tests: `test_mock_scenarios.py`, `test_mock_host.py`,
`test_mock_tui.py`, `test_mock_llm_serve.py`. Plan: `plans/MOCK_PLAN.md`.

## Benchmark (`benchmark/`)

`benchmark/bench.py` runs two small **real-model** scenarios against the configured
provider: `file-edit` (write `result.txt` with `color=green`; graded on exact bytes)
and `shell-command` (exact `echo shell-ok > result.txt; echo BENCHMARK_BASH_RAN`;
graded on the file plus JSONL evidence of exactly one matching `bash` call).

```sh
python3 benchmark/bench.py list | setup [NAME] | run file-edit | run shell-command --allow-shell | check [NAME] | reset [NAME]
```

- Setup copies your config into each generated workspace but replaces permissions
  with a deny-by-default allowlist; permissive modes, inherited grants, unbounded
  write roots, unattended prompts, literal credentials are refused (use
  `${env:VAR}` or the credential store). Needs a v2 config.
- `--timeout` defaults to 300s, capped at 600. Output and run logs live under
  `artifacts/benchmark/` (ignored); `reset` only removes marked benchmark
  directories and refuses symlinks.
- Exit zero requires a clean process exit **and** a passing grade. Grading uses
  files and tool events, not the model's final text.
- Tests: `tests/test_benchmark.py`. Details: `benchmark/README.md`.

The benchmark workspace also includes an offline extension smoke fixture:
`benchmark/mcp_echo.py`, `.agents/mcp.json`, and the `benchmark-echo` skill
under `.agents/skills/`. With `benchmark/` selected as the workspace, the skill
loads through `skill` and calls `mcp__echo__echo`, returning `benchmark-mcp-ok`.
The runtime integration check uses a scripted model and a real stdio subprocess:
`pytest -q tests/test_mcp_integration.py -k benchmark_skill`. It launches from
outside the workspace to verify relative MCP script resolution.

### Code-mode evaluation (offline, plan 03 E1)

`benchmark/codemode_eval.py` compares two **hand-scripted strategies** through the
real `Runtime` (real tools, `PathGuard`, permissions, nested pipeline; a real stdio
MCP subprocess for the MCP task) with a `ScriptedProvider`: *direct* tool calls
versus one `code` program. No network, no provider, no credentials.

```sh
.venv/bin/python benchmark/codemode_eval.py [--scenario NAME]... [--json]
.venv/bin/python -m pytest -q tests/test_codemode_eval.py
```

| Scenario | Direct arm | Code arm |
| --- | --- | --- |
| `fanout_read` | 30 parallel `read` calls | one program gathers 30 reads, prints only the TODO lines |
| `fanout_grep` | 12 parallel `grep` calls (8 narrow, 4 broad) | one program gathers them, prints a count and first hit each |
| `mcp_list_filter` | `list_records` (200 summary rows, `benchmark/mcp_records.py`), then 4 `get_record` | one program lists, filters open and critical, fetches 4 |
| `long_child_context` | none (a root has no tool that reads a child transcript) | child reads 24 files; the root queries it with `context_search/tool_calls/meta/report` |

Output per arm: model requests, top-level and nested tool calls, bytes of tool
results that enter the model's context (UTF-8, not tokens), bytes of tool-call
inputs, bytes of tool definitions in the first request, tool errors, and a content
grade (required strings must appear in what the model sees; the MCP code arm must
not leak closed issues). For `long_child_context`: the child's tool-result bytes
against the bytes the root's model receives. The exit status is non-zero if any arm
fails its grade.

**Interpretation.** The numbers are deterministic and mechanical: they show that
*when a program is written correctly*, code mode keeps bulk tool output out of the
model's context and saves model round trips, with every nested call still visible
as `code.call` events. They do **not** show that a live model chooses code mode
when it should, writes working programs, recovers from Monty's language limits, or
gets better answers; the scripted "model" never errs. The code arm also pays a
fixed cost of larger tool definitions (the `code` catalog), reported as
`tool_definition_bytes` (direct arm: its own tool list, measured the same way).
The long-child task proves the read path, bounding and permission flow, not that
RLM-style recursion helps a model. Live trials (one per supported provider, and a
live long-child trial) are not done and are the basis for default-exposure and
U7/U8 decisions, which stay at the current defaults (`expose = "read_only"`, no `only`
mode, no `llm()` in plain code mode). `code_mode.enabled` defaults to `true` by user
decision, not from live trial evidence.

Code arms need `pydantic-monty==1.1.0` (a base dependency); without it they are
reported `skipped` (tests skip) and the direct arm still runs.

## Local search (`websearch/`)

A Docker Compose SearXNG bound to `127.0.0.1:18765` for the `websearch` tool
(`nexus searchserver start` uses the packaged copy in
`nexus/host_support/searchserver/`). Needs Docker and a free port; a private
`.env` holds `SEARXNG_SECRET` (Git-ignored). `websearch/README.md` has the manual
steps.

## Examples (`examples/`)

| File | Shows |
| --- | --- |
| `python_api.py` | drive `HostFacade` in-process with a `ScriptedProvider` (no daemon, no credentials); approves a gated write and prints the view |
| `nexus.toml` | a full layered config sample (`ext.dirs` and `agents.default_type` there predate the `.agents/` and `task` defaults; [config.md](config.md) is authoritative) |
| `tools/greet.py`, `skills/hello-world/SKILL.md`, `agents/researcher.md`, `hooks.toml`, `mcp.json` | minimal extension samples ([extending.md](extending.md)) |

## Repository scripts

- `scripts/hooks/commit-msg`: Conventional Commits check; enable once per clone
  with `git config core.hooksPath scripts/hooks` ([release.md](release.md)).
- `install.sh`, `install.ps1`: installers ([release.md](release.md)); design in
  `plans/install.md`.

## Ratatui design mock-ups

`design-mockups/` is a standalone Cargo project that renders every native-TUI
screen and state from fake data using the real component kit in `rust/widgets`
(`nexus-widgets`). It is a design and UX prototype, not product code: it never
talks to the daemon, and deleting it removes nothing the TUI needs. Run
`cargo run` (viewer), `cargo run -- shoot` (screenshots and `shots/index.html`)
and `cargo test` in that folder; see its README. The spec and phase checklist are
in `plans/done/RATATUI_DESIGN_MOCKUPS_PLAN.md`. `rust/tui` does not use the kit yet
(Phase 3); not verified against a real terminal beyond the SVG/text renders.
