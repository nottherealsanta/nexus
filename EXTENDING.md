# Extending Nexus

Everything here is a first-class extension mechanism, not a plugin API. There
are two tiers:

- **Data** — skills, agents, hooks, MCP servers, `nexus.toml`, `SOUL.md`,
  `MEMORY.md`. These are parsed, not imported, and reload freely.
- **Code** — `.py` tools and in-process hooks. These are loaded through
  quarantine under a generation-stamped module name.

Read the honesty note at the end before you load any code extension.

## A custom tool

Create `.nexus/tools/my_tool.py`. A tool module declares either a module-level
`SPEC` plus `async def run(args, ctx)`, or a synchronous `register()` returning
tool declarations. The two are mutually exclusive.

```python
from typing import Any

from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="MetricsQuery",
    description="Query the metrics API for a single time-series aggregate.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PromQL expression."},
            "window": {"type": "string", "description": "e.g. 24h."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    bundle="ext",          # fs | shell | task | meta | ext
    mutates=False,         # mutating tools run exclusively and are gated
    timeout_s=15.0,
)


async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:
    query = args.get("query")
    if not isinstance(query, str) or not query:
        return ToolExecutionResult.text("'query' is required", is_error=True)
    # ctx.workspace, ctx.session_id, ctx.turn_id, ctx.emit(...) are available.
    # ctx never exposes the Runtime.
    return ToolExecutionResult.text(f"p99 latency for {query}: 412ms")
```

Rules:

- `name` must match `[A-Za-z][A-Za-z0-9_]{0,63}` and must not collide with a
  builtin.
- `input_schema` must be a JSON Schema object with `type: object`.
- A file whose name starts with `_` is treated as a support module and never
  loaded (this is how the seeded `_template.py` works).
- The active profile must include your bundle. In the default `coding` profile,
  `ext` and `meta` are enabled, so the model can call `ReloadExtensions()` and
  then your tool in the same turn.

Call `ReloadExtensions()` (or edit the file and let the watcher fire) to make it
live; `nexus ext reload` runs the same rebuild from the CLI, and
`nexus ext validate [target]` quarantine-checks without swapping the manifest.
The report names what loaded and what failed. A failed load leaves the previous
manifest untouched and returns the error to the model.

To remove a loaded extension safely, use `nexus ext trash <path> [--reason R]
[--force]`. It is scoped to managed roots, refuses symlinks, traversal, and
non-candidates, moves the file into the extension trash with a retention record,
and rebuilds. A failed rebuild rolls the move back unless `--force` is passed.
There is no CLI restore; each entry carries a `delete_after` retention deadline
that `purge_expired` enforces.

`register()` form, for modules that expose several tools:

```python
from nexus.tools.spec import ToolSpec

def register():
    return [MyFirstTool, MySecondTool]   # each has .spec and async .run
```

## A custom provider

An adapter is one file implementing the `Provider` protocol against a wire
protocol. Drop it in `.nexus/providers/<name>.py` (workspace) or
`~/.nexus/providers/<name>.py` (user); discovery validates and loads it through
the same quarantine path as tools. A file defines one of `PROVIDER` (one
provider object), `PROVIDERS` (a name-to-provider mapping), or
`build(context)` (a callable returning either; `context` carries the runtime's
already-built transport kwargs). A file whose name is already a configured
`[providers.<name>]` section is ignored: config wins.

```python
class Provider(Protocol):
    name: str
    def capabilities(self, model: str) -> Capabilities: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: ModelRequest) -> int | None: ...
    async def aclose(self) -> None: ...
```

Use the shared `nexus.model.http.HTTPTransport` for pooling, retry/backoff, and
SSE framing, and the shared `ToolCallAccumulator` for partial-JSON tool
arguments. Capabilities should come from the injected registry source when one
is supplied; an in-module table is only the fallback.

Then select it with a config block:

```toml
[providers.acme]
kind = "openai_compatible"   # no new code needed if it speaks this
base_url = "https://api.acme.example/v1"
api_key = "${env:ACME_API_KEY}"
```

Pass the provider conformance suite (`tests/provider_conformance/`) against a
recorded fixture before relying on it. If it passes, the loop will drive it.

## A custom skill

`.nexus/skills/<dir>/SKILL.md`. The six supported keys are exactly `name`,
`description`, `allowed-tools`, `bundles`, `model`, `version`; `name` and
`description` are required.

```markdown
---
name: release-check
description: Verify the release checklist before tagging. Use when asked to cut a release.
allowed-tools: [Bash, Read, Glob]
bundles: [fs, shell]
model: inherit
version: 1
---

## Steps
1. Run `pytest`.
2. Confirm the changelog has an entry under Unreleased.
3. Tag only after both pass.
```

Only `name: description` enters the context until the model invokes the skill;
then the body is returned as a tool result. A skill may bundle `scripts/`,
`references/`, and `tools/*.py`; bundled tools register only while the skill is
active. Precedence is workspace > user > builtin.

## A custom agent

`.nexus/agents/<name>.md`. The frontmatter grammar is deliberately restricted
(no YAML library): the seven keys are `name`, `description`, `bundles`, `tools`,
`model`, `max_iterations`, `context_tokens`. A `tools` item may carry a leading
`-` to exclude.

```markdown
---
name: security-reviewer
description: Read-only reviewer for auth and secrets handling.
bundles: [fs]
tools: [-Write, -Edit, -MultiEdit, -Bash]
model: high
max_iterations: 20
---

You review code for authentication, authorization, and secret-handling
mistakes. Return concrete findings with file and line references, ranked by
severity. Do not modify files; the parent will act on your report.
```

The parent spawns it with `Task(subagent_type="security-reviewer", prompt="...")`
or as an ad-hoc agent with an explicit `tools=` list. A child can never exceed
its parent: tools intersect, permissions inherit, `deny` stays absolute, the
tier is clamped by `agents.max_tier`, and depth/fan-out are bounded. The
`explore` and `planner` roles are structurally read-only; naming one of those
names makes a definition read-only regardless of what it declares.

## A custom hook

Declarative command hooks live in `.nexus/hooks.toml`:

```toml
[[hooks.PreToolUse]]
matcher = "Write(**/*.py)"          # the permission-rule grammar
type = "command"
command = ["ruff", "check", "-"]    # argv; no shell unless shell = true
on_nonzero = "block"                # block | warn | ignore
timeout_s = 10
```

A command hook receives the invocation as JSON on stdin and a bounded `NEXUS_*`
environment (`NEXUS_HOOK_EVENT`, `NEXUS_TOOL_NAME`, `NEXUS_TOOL_KEY`,
`NEXUS_TOOL_PATH`, `NEXUS_TOOL_BUNDLE`, `NEXUS_SESSION_ID`, `NEXUS_TURN_ID`),
and a fixed safe OS environment. Set `shell = true` and `command = "..."` only
when you deliberately want a shell.

In-process Python hooks live in `.nexus/hooks/*.py` and declare `HOOKS` or a
synchronous `register()` returning an iterable of declarations. Each declaration
names an event and carries a `run(invocation, ctx)` callable:

```python
from nexus.hooks.model import HookDecision


def _no_todos(invocation, ctx):
    if "TODO" in str(invocation.tool_input.get("content", "")):
        return HookDecision.block("TODO markers are not allowed")
    return HookDecision.allow()


def register():
    return [
        {
            "event": "PreToolUse",
            "name": "no-todos",
            "matcher": "Write(**)",   # optional; the permission-rule grammar
            "run": _no_todos,
        }
    ]
```

Events: `SessionStart`, `UserPromptSubmit`, `ContextAssembled`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `TurnEnd`, `SessionEnd`, `ExtensionLoaded`.
Decisions: `allow`, `warn(reason)`, `block(reason)`, `modify(new_input)`. A
`modify` must be revalidated and re-gated by the caller — a hook is policy, not
a permission grant. A block becomes an error tool result the model sees.

## An MCP server

`.nexus/mcp.json` (JSONC: comments and trailing commas are accepted):

```jsonc
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    },
    "internal": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:MCP_TOKEN}" }
    }
  }
}
```

Keys per server: `transport` (`stdio` | `http` | `sse`), `command`, `args`,
`env`, `cwd`, `url`, `headers`, and the four `*_timeout_s` values. Unknown keys
are an error. Only `${env:VAR}` interpolation is performed; a bare `$VAR` is
left literal. Editing the file takes effect live.

Bridged tools appear as `mcp__<server>__<tool>` in the `mcp` bundle and are
gated like any other tool. Server stderr is captured to `.nexus/logs/mcp/`,
never into context. Tool descriptions and results are untrusted data.

## Config and permissions

Add rules in `nexus.toml` without restating the user's list (lists append across
layers):

```toml
[permissions]
mode = "ask"
allow = ["Read(**)", "Glob(**)", "Grep(**)", "LS(**)", "Bash(git status)", "Bash(git diff*)"]
ask   = ["Write(**)", "Edit(**)"]
deny  = ["Bash(rm -rf*)", "Read(**/.env)", "Read(**/credentials*)"]
write_roots = ["./"]
read_denyroots = ["~/.ssh", "~/.nexus/credentials.json"]
on_unattended = "deny"
```

`deny` is absolute. A `~` in a rule *pattern* is rejected because tool
permission keys are canonical paths; use absolute paths or `read_denyroots` for
home locations. A keyed grant persists only as an exact-action rule, so an
`ALLOW_ALWAYS` cannot widen.

`[ext]`, `[agents]`, `[hooks]`, and `[mcp]` tune the extension subsystems:

```toml
[ext]
enabled = true
watch_interval_ms = 500   # 0 disables the watcher
quarantine = true
max_file_bytes = 262144

[agents]
max_tier = "medium"
max_concurrent = 4
max_depth = 3
```

## A UI renderer

A UI sits above the facade and may import only `nexus.host`, `nexus.view`,
`nexus.events`, and the standard library. Render from the `view/` model rather
than reinterpreting event types: both the CLI and a future HTTP surface fold the
same events with the same reducer, so identical semantics are guaranteed by
construction. Registering a per-tool or per-event renderer above the facade is
supported; a bad renderer breaks a pane, never a turn. There is deliberately no
UI plugin API that receives a `Runtime`, a manager, or a tool.

## The honesty note

Quarantine reads a file under a size cap, `ast.parse`s it, scans for import-time
side effects, imports it in an isolated subprocess with a hard timeout, validates
the declared spec and schema, and re-hashes the staged bytes immediately before
the in-process import. It catches syntax errors, import crashes, hangs, and
obvious side effects.

It does **not** sandbox arbitrary Python. Extension files are **trusted code**,
exactly as `nexus.toml` and `SOUL.md` are. The permission engine gates *calls*,
not *loading*. Only enable extensions you have reviewed, keep `ext` bundle tools
behind `ask` (the default), and set `ext.enabled = false` in hostile contexts.
