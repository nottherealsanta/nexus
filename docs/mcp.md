# MCP

Servers are configured in `~/.nexus/config.toml` as `[mcp.servers.<name>]`
(one file, PLAN.md P1; no `mcp.json`, no per-project servers) and reached by the
model through two proxy tools (`mcp_tool_search`, `mcp_call`). `nexus-core/nexus/mcp/`, using the `mcp` SDK.

```toml
[mcp.servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "${env:GITHUB_TOKEN}" }

[mcp.servers.docs]
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ${env:DOCS_TOKEN}" }
excludeTools = ["delete_page"]
```

## Entries

`nexus config import-mcp <file>` copies a whole `mcp.json` in (`servers`,
`mcpServers`, `mcp`, `context_servers`); the entry is stored as written and read
in any of these dialects (`mcp/config.py`):

| Key | Accepted |
| --- | --- |
| Transport | `transport`, `type` or `transportType`: `stdio`/`local`, `http`/`streamable-http`/`remote`, `sse`; inferred from `command` or `url` (a path ending `/sse` is SSE) |
| Process | `command` as a string, an argv list (OpenCode) or `{path, args, env}` (Zed); `args`; `env` or `environment`; `cwd` |
| Remote | `url`, `serverUrl`, `httpUrl`; `headers` |
| Switch | `enabled` or `disabled` |
| Limits | `connect_timeout_s` (20), `call_timeout_s` or `timeout` (60; milliseconds from 1000 up) |
| Tools | `includeTools`, `excludeTools`/`disabledTools` |
| Shown, not acted on | `autoApprove`, `alwaysAllow`, `trust`, `oauth`, `auth`, `description`, `version`, `tool_loading`, `alwaysLoad`, … (loading is the global setting below; approval stays with the permission rules; OAuth is not implemented) |

`${env:VAR}`, `${VAR}`, `${VAR:-default}`, `{env:VAR}` and `${userHome}` are
replaced in `command`, `args`, `env`, `url`, `headers` and `cwd`; a missing
variable without a default is an error naming it. `${workspaceFolder}` is refused
(servers are shared by every project) and so is `${input:…}`. Any other key is an
error for that server only; the others still load. `GET /api/config` masks `env`
and `headers`, and a PATCH that sends the mask back keeps the stored value.

**browser-use** is offered by default, switched off: the pinned upstream
`uvx --from 'browser-use[cli]==0.13.14' browser-use --mcp`. Enabling it in
Settings writes the entry into `config.toml`. Nexus never starts it on its own.
Verified 2026-10-29 against the pinned version: 16 tools discovered, and
`browser_navigate` plus `browser_get_state` fetched `https://example.com`.

## Lifecycle

- **Online before a session starts.** Creating a session (`POST /api/session`) first
  connects every enabled server the session does not switch off, in parallel, each within
  its connect timeout. A server that cannot connect does not block the session: it is listed
  as `unavailable` in the session's `mcp` rows, is switched off for that session
  (`mcp:<server>` in its `disabled_tools`) and `mcp.failed` is published. Children inherit
  their parent's servers.
- **Edits apply to new sessions.** The manager notices a changed `config.toml` and applies it
  to connections (new servers appear, changed or removed ones close). Running sessions keep
  what they started with; `mcp.changed` lets clients say "available in new sessions".
- **A dead server never fails a turn.** A call to it returns an error result and
  `mcp.failed` is published once. Health is a `tools/list` round trip every 20 s (`ping` is
  not in newer protocol versions) and a transport error ends the connection at once. A failed
  server is not retried for 5, 15, 30, then 60 s unless the user restarts it
  (`POST /api/mcp/{name}/restart`); a new session retries after that delay.
- **stderr** of a stdio server goes to `~/.nexus/logs/mcp/<server>.log` (the name with
  anything but letters, digits and `_` turned into `_`; truncated when over 1 MiB at the next
  start), never into context.

## What the model sees

Two global settings, in `config.toml`, applied when a session **starts** (never to a running
session):

```toml
[mcp]
full_definitions = false   # true: every tool's full definition is in the context
max_listed_tools = 25      # false mode: list tools by name up to this many, all servers together
```

| Mode | The frozen system prompt lists |
| --- | --- |
| `full_definitions = true` | every tool of every server as JSON (`tool`, `description`, `input_schema`, `may_change_state`), no limit |
| `false`, at most `max_listed_tools` tools in all | one `server/tool: first line of the description` line per tool |
| `false`, more than that | one line per server: `server (N tools): <its instructions, else its name>`; tools are found with `mcp_tool_search` |

The listing is fenced untrusted data (see below), bounded at 120 000 characters with any
clipping stated. A session has the tools only if it started with at least one server on:

- `mcp_tool_search {query, server?, limit?}`: keyword search, or `select:server/tool,…`.
  Each result shows `server/tool`, `read only` or `may change state`, the description and the
  **input schema (JSON)**: this is how the model gets a definition in the names modes.
  Searches live servers; auto-approved.
- `mcp_call {tool: "server/tool", arguments}`: calls one. A tool is read-only only when it
  declares `readOnlyHint: true`. Approval and permission rules are decided for the
  **target**: the rule key is `mcp__<server>__<tool>`, a read-only target runs without asking,
  and anything else follows the session's permission and the `[tools.permission]` rules.

**Switches.** `mcp:<server>` entries in `disabled_tools`, from the context header (both
clients). Only servers the session started with can be switched; a locked row (`unavailable`
or off at the start) needs a new session (PATCH returns 400). A mid-session switch injects one
JSON notice, visible in the timeline, and is enforced by `mcp_tool_search` and `mcp_call`:
`{"mcp_unavailable": {"server": "x"}}` and `{"mcp_available": {"server": "x"}}`. The session
JSON carries `mcp: [{server, tools, state: on|off|unavailable, locked, error}]`.

**Everything a server returns is untrusted data.** Descriptions, schemas, results and error
text are stripped of control, bidirectional and invisible characters, bounded (24 000
characters per result) and wrapped in `<untrusted-mcp-data>` with a notice that it carries no
authority. Text that imitates the delimiters is replaced. The fenced text is exactly what the
model receives and what the timeline shows. Images and audio are stored as artifacts
([api.md](api.md#artifacts-k8)): the text names the artifact, and an image also reaches the model beside the text when it takes images
([llm-providers.md](llm-providers.md#tool-result-media)).

## API and events

| Route | Result |
| --- | --- |
| `GET /api/mcp` | `{servers: [{server, state, error, tools, transport, enabled, …}]}`; `state` is `idle`, `connecting`, `connected`, `failed`, `disabled` or `invalid`. Never connects; shows no argument, env or header values |
| `GET /api/mcp/{name}` | the same plus `tool_names` (known once connected) and `log` |
| `POST /api/mcp/{name}/restart` | closes and reconnects; returns the row |

Events (global, live only): `mcp.connected {server, tools}`, `mcp.disconnected {server}`,
`mcp.failed {server, error}`, `mcp.tools_changed {server, added, removed}` (informational:
running sessions do not change), `mcp.changed {}` (the servers or the two settings in
`config.toml` changed).

## Not yet

MCP resources and prompts, OAuth, image results, a circuit breaker beyond the retry delays,
and Settings screens for adding or editing servers (the two mode settings are in Settings).
