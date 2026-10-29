# MCP

Servers are configured in `~/.nexus10/config.toml` as `[mcp.servers.<name>]`
(one file, PLAN.md P1; no `mcp.json`, no per-project servers) and reached by the
model through two proxy tools. `nexus-core/nexus/mcp/`, using the `mcp` SDK.

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
| Tools | `tool_loading` (`search`; `all` is not built yet), `includeTools`, `excludeTools`/`disabledTools` |
| Shown, not acted on | `autoApprove`, `alwaysAllow`, `trust`, `oauth`, `auth`, `description`, `version`, … (approval stays with the permission rules; OAuth is not implemented) |

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

- **Lazy.** Nothing connects at daemon start or session creation. The first
  `mcp_search` or `mcp_call` that needs a server connects it, once, however many
  callers wait. Each connection runs in its own task.
- **Edits apply on next use.** The manager notices a changed `config.toml`; new
  servers appear, changed or removed ones close, unchanged ones keep running.
- **A dead server never fails a turn.** A call to it returns an error result and
  `mcp.failed` is published once. Health is a `tools/list` round trip every 20 s
  (`ping` is not in newer protocol versions) and a transport error ends the
  connection at once. A failed server is not retried for 5, 15, 30, then 60 s
  unless the user restarts it (`POST /api/mcp/{name}/restart`).
- **stderr** of a stdio server goes to `~/.nexus10/logs/mcp/<server>.log` (the
  name with anything but letters, digits and `_` turned into `_`; truncated when
  over 1 MiB at the next start), never into context.

## What the model sees

A session whose start found an enabled server in `search` mode gets two tools
(a session without one has neither, like skills):

- `mcp_search {query, server?, limit?}`: keyword search over the connected
  servers' tools, or `select:server/tool,…`. Each result shows `server/tool`,
  `read only` or `may change state`, the description and the input schema
  (clipped at 8000 characters; `select:` shows one alone). This connects any
  server it needs and is auto-approved.
- `mcp_call {tool: "server/tool", arguments}`: calls one. A tool is read-only
  only when it declares `readOnlyHint: true`. Approval and permission rules are
  decided for the **target**: the rule key is `mcp__<server>__<tool>`, a
  read-only target runs without asking, and anything else follows the session's
  permission setting and the `[tools.permission]` rules.

The system prompt lists the server names (frozen at the session's start). The
tools themselves are live: a server's tools may change while a session runs, and
`mcp.tools_changed` says so. Per-session switches are `mcp:<server>` entries in
`disabled_tools`; a switched-off server is left out of a new session and refused
by a running one.

**Everything a server returns is untrusted data.** Descriptions, schemas,
results and error text are stripped of control, bidirectional and invisible
characters, bounded (24 000 characters; clipping is stated) and wrapped in
`<untrusted-mcp-data>` with a notice that it carries no authority. Text that
imitates the delimiters is replaced. The fenced text is exactly what the model
receives and what the timeline shows. Images and audio are not kept anywhere
(artifact storage is PLAN.md K8): each becomes a labelled placeholder.

## API and events

| Route | Result |
| --- | --- |
| `GET /api/mcp` | `{servers: [{server, state, error, tools, transport, enabled, tool_loading, …}]}`; `state` is `idle`, `connecting`, `connected`, `failed`, `disabled` or `invalid`. Never connects; shows no argument, env or header values |
| `GET /api/mcp/{name}` | the same plus `tool_names` (known once connected) and `log` |
| `POST /api/mcp/{name}/restart` | closes and reconnects; returns the row |

Events (global, live only): `mcp.connected {server, tools}`,
`mcp.disconnected {server}`, `mcp.failed {server, error}`,
`mcp.tools_changed {server, added, removed}`.

## Not yet

`tool_loading = "all"` (full schemas in every request, with a notice when a
server's list changes), MCP resources and prompts, OAuth, image results,
Settings screens in both clients, and a circuit breaker beyond the retry delays.
