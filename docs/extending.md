# Extending Nexus

Recipes for the extension mechanisms. Everything here is first-class, not a
plugin API. **Data** extensions (skills, agents, hooks, MCP servers, config,
`SOUL.md`/`MEMORY.md`) are parsed and reload freely; **code** extensions (`.py`
tools, in-process hooks, file providers) are loaded through quarantine under a
generation-stamped module name. Read [the trust note](#the-trust-note) before
loading any code. How loading works: [extensions.md](extensions.md).

Project extensions live under `<workspace>/.agents/`; user-scope ones under
`~/.nexus/`. A legacy `<workspace>/.nexus/` is read as a lower-precedence
fallback, and writes always go to `.agents/`. Samples are in
[`examples/`](../examples/).

## Choosing the mechanism

| I want to… | Use |
| --- | --- |
| give the model a new capability | a tool (`.py`) or an MCP server |
| give the model reusable instructions or a workflow | a skill |
| change who does a kind of work (prompt, model, tools) | an agent definition |
| enforce a rule the model cannot skip | a hook |
| add a model vendor | a `[providers.*]` block; else a provider file |
| add a wire protocol | an adapter in `nexus/model/providers/` ([provider-onboarding.md](provider-onboarding.md)) |
| change core behavior | a code change in the owning layer ([architecture.md](architecture.md)) |

## A custom tool

`.agents/tools/my_tool.py` declares either a module-level `SPEC` plus
`async def run(args, ctx)`, or a synchronous `register()` returning tool
declarations (mutually exclusive).

```python
from typing import Any
from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="MetricsQuery",
    description="Query the metrics API for a single time-series aggregate.",
    input_schema={"type": "object",
                  "properties": {"query": {"type": "string", "description": "PromQL expression."}},
                  "required": ["query"], "additionalProperties": False},
    bundle="ext",        # the active profile must include it (coding includes ext)
    group="metrics",     # optional label in the context header; no effect on permissions
    mutates=False,       # True ⇒ runs exclusively and is gated
    timeout_s=15.0,
)

async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:
    query = args.get("query")
    if not isinstance(query, str) or not query:
        return ToolExecutionResult.text("'query' is required", is_error=True)
    return ToolExecutionResult.text(f"p99 latency for {query}: 412ms")
```

Rules: name `[A-Za-z][A-Za-z0-9_]{0,63}` and not a built-in name; `input_schema` a
JSON Schema object; a file starting with `_` is a support module and never loaded
(`_template.py` is seeded for the model to read); `ctx` has `workspace`,
`session_id`, `turn_id`, `emit(...)` and narrow services, never the `Runtime`.
Make it live with `ReloadExtensions` (or let the watcher fire; `nexus ext reload`;
`nexus ext validate [target]` checks without swapping). A failed load keeps the
previous manifest and returns the error to the model. Remove safely with
`nexus ext trash <path> [--reason R] [--force]`. The `register()` form returns
several registered tools (each with `.spec` and async `.run`).

## A custom provider

If the vendor speaks OpenAI-compatible HTTP, add a config block, no code:

```toml
[providers.acme]
kind = "openai_compatible"
base_url = "https://api.acme.example/v1"
api_key = "${env:ACME_API_KEY}"
```

Otherwise drop a file in `.agents/providers/<name>.py` or
`~/.nexus/providers/<name>.py` defining `PROVIDER`, `PROVIDERS` (a name → provider
mapping) or `build(context)`; it loads through the same quarantine path. A file
named like a configured `[providers.<name>]` is ignored: config wins. Implement
`name`, `capabilities(model)`, `stream(req)`, `count_tokens(req)`, `aclose()`; use
`nexus.model.http.HTTPTransport` (pooling, retry, SSE) and `ToolCallAccumulator`;
take capabilities from the injected registry source, with a table only as a
fallback. Pass the conformance suite (`python -m tests.provider_conformance`) on a
recorded fixture before relying on it.

## A custom skill

`.agents/skills/<dir>/SKILL.md`. Six keys: `name`, `description`, `allowed-tools`,
`bundles`, `model`, `version` (`name` and `description` required).

```markdown
---
name: release-check
description: Verify the release checklist before tagging. Use when asked to cut a release.
allowed-tools: [bash, read, glob]
bundles: [fs, shell]
model: inherit
version: 1
---

## Steps
1. Run `pytest`.
2. Confirm the changelog has an entry under Unreleased.
```

Only `name: description` enters context until the model calls `skill`; then the
body is returned as a tool result. A skill may bundle `scripts/`, `references/`
and `tools/*.py` (registered only while active). `allowed-tools` narrows, never
grants. Precedence: workspace > user > builtin.

## A custom agent

`.agents/agents/<name>.md` (grammar and keys: [agents.md](agents.md)):

```markdown
---
name: security-reviewer
description: Read-only reviewer for auth and secrets handling.
bundles: [fs]
tools: [-write, -edit, -apply_patch, -bash]
model: high
reasoning_effort: high
color: #4F8EF7
max_iterations: 20
---

You review code for authentication, authorization and secret-handling mistakes.
Return concrete findings with file and line references, ranked by severity. Do
not modify files; the parent will act on your report.
```

The parent spawns it with `subagent(subagent_type="security-reviewer", prompt="…")`
or an ad-hoc agent with an explicit `tools=` list. A child never exceeds its
parent. Naming a definition `advisor` (or legacy `explore`/`plan`/`planner`)
makes it structurally read-only. A definition with a built-in's name (`build`,
`advisor`, `task`, `quick`, `orchestrator`) overrides it.

## A custom hook

Command hooks in `.agents/hooks.toml`:

```toml
[[hooks.PreToolUse]]
name = "lint-python"
matcher = "write(**/*.py)"          # permission-rule grammar
type = "command"
command = ["ruff", "check", "-"]    # argv; no shell unless shell = true
on_nonzero = "block"                # block | warn | ignore
timeout_s = 10
```

The hook gets the invocation as JSON on stdin and a bounded `NEXUS_*` environment.
Python hooks: `.agents/hooks/*.py` with `HOOKS` or `register()` returning
declarations `{"event", "name", "matcher"?, "run"}`; `run(invocation, ctx)` returns
`HookDecision.allow() | warn(reason) | block(reason) | modify(new_input)`. Events:
`SessionStart`, `UserPromptSubmit`, `ContextAssembled`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `TurnEnd`, `SessionEnd`, `ExtensionLoaded`. A `modify`
is re-validated and re-gated; a block becomes an error result the model sees.

## An MCP server

`.agents/mcp.json` (JSONC; `servers` or `mcpServers`):

```jsonc
{ "servers": {
    "filesystem": { "transport": "stdio", "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] },
    "internal":   { "transport": "http", "url": "https://mcp.example.com/mcp",
                    "headers": { "Authorization": "Bearer ${env:MCP_TOKEN}" } } } }
```

Per server: `transport` (`stdio|http|sse`), `command`, `args`, `env`, `cwd`, `url`,
`headers`, `*_timeout_s`; unknown keys are errors; only `${env:VAR}` interpolates.
Edits apply live. Add `"tool_loading": "all"` to load a server's tools directly
as `mcp__<server>__<tool>`; the default `"search"` keeps their schemas out of the
request and the agent finds them with `McpSearch` and runs them with `McpCall`.
All MCP tools live in bundle `mcp`, are gated like any tool by their target's
name, and their descriptions and results are untrusted.

## Config and permissions

Lists append across layers, so a project adds rules without restating the user's:

```toml
[permissions]
mode = "ask"
allow = ["read(**)", "glob(**)", "grep(**)", "bash(git status)", "bash(git diff*)"]
ask   = ["write(**)", "edit(**)"]
deny  = ["bash(rm -rf*)", "read(**/.env)", "read(**/credentials*)"]
write_roots = ["./"]
read_denyroots = ["~/.ssh", "~/.nexus/credentials.json"]
on_unattended = "deny"
```

`deny` is absolute; a `~` in a rule *pattern* is rejected (use absolute paths or
`read_denyroots`). Legacy capitalised names (`Bash(...)`) are still accepted.
Other sections: [config.md](config.md).

## A UI renderer

A UI sits above the facade. Render from the `view/` model instead of
reinterpreting events; there is deliberately no UI plugin API that receives a
`Runtime`, manager or tool. See [surfaces.md](surfaces.md) and [host.md](host.md).

## A voice engine

Implement the `Engine` protocol in `nexus/voice/` ([voice.md](voice.md)).

## The trust note

Quarantine reads a file under a size cap, `ast.parse`s it, scans for import-time
side effects, imports it in an isolated subprocess with a hard timeout, validates
the declared spec and schema, and re-hashes the staged bytes immediately before
import. It **does not sandbox** Python: a loaded extension runs with the
harness's privileges. Only load code you have reviewed.
