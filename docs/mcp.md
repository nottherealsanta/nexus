# MCP

MCP servers come from two scopes, and the model reaches both through the same
two proxy tools (`mcp_tool_search`, `mcp_call`). Code:
`nexus-core/nexus/mcp/`, using the `mcp` SDK.

- **Global** servers are the user's configuration in `~/.nexus/config.toml`
  as `[mcp.servers.<name>]`. They need no approval.
- **Project** servers are `<project>/.mcp.json`. They are trusted configuration:
  every enabled, valid entry starts with a new session in that project, with no
  approval prompt.

> **Warning: `<project>/.mcp.json` is arbitrary code.** Opening a project does
> not run it, but starting a session in that project starts each enabled server
> it names (a stdio `command` and its arguments), with the user's permissions and
> no sandbox. A committed `.mcp.json` may come from someone else, and a project
> directory is not a security boundary. Read the file before starting a session
> in an untrusted project. Disabling an entry (`"enabled": false` or
> `"disabled": true`) is the only way to keep it from starting.

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

`nexus config import-mcp <file>` copies a whole `mcp.json` into the global
table (`servers`, `mcpServers`, `mcp`, `context_servers`); the entry is stored
as written and read in any of these dialects (`mcp/config.py`):

| Key | Accepted |
| --- | --- |
| Transport | `transport`, `type` or `transportType`: `stdio`/`local`, `http`/`streamable-http`/`remote`, `sse`; inferred from `command` or `url` (a path ending `/sse` is SSE) |
| Process | `command` as a string, an argv list (OpenCode) or `{path, args, env}` (Zed); `args`; `env` or `environment`; `cwd` |
| Remote | `url`, `serverUrl`, `httpUrl`; `headers` |
| Switch | `enabled` or `disabled` |
| Limits | `connect_timeout_s` (20), `call_timeout_s` or `timeout` (60; milliseconds from 1000 up, capped at 600 s) |
| Tools | `includeTools`, `excludeTools`/`disabledTools` |
| Shown, not acted on | `autoApprove`, `alwaysAllow`, `trust`, `oauth`, `auth`, `description`, `version`, `tool_loading`, `alwaysLoad`, … (loading is the global setting below; a tool call still follows the permission rules; OAuth is not implemented) |

`${env:VAR}`, `${VAR}`, `${VAR:-default}`, `{env:VAR}`, `${userHome}`,
`${pathSeparator}` and `${/}` are replaced in `command`, `args`, `env`, `url`,
`headers` and `cwd`; a missing variable without a default is an error naming it.
`${workspaceFolder}` and `${workspaceFolderBasename}` are refused in **global**
entries (servers are shared by every project) and allowed in **project**
entries. `${input:…}` is refused everywhere. Any other key is an error for that
server only; the others still load. `GET /api/config` masks global `env` and
`headers`, and a PATCH that sends the mask back keeps the stored value.

**browser-use** is offered by default, switched off: the pinned upstream
`uvx --from 'browser-use[cli]==0.13.14' browser-use --mcp`. Enabling it in
Settings writes the entry into `config.toml`. Nexus never starts it on its own.
Verified 2026-10-29 against the pinned version: 16 tools discovered, and
`browser_navigate` plus `browser_get_state` fetched `https://example.com`.

## Project servers

Only `<project>/.mcp.json` is read: the file at the project record's path, not
`.agents/mcp.json`, not a nested file, and not the directory a session has moved
to. Nexus does not merge it with the global table. A worktree session still uses
its project's `.mcp.json`, and a stdio entry's default working directory is the
project path (not the session's current directory).

The file accepts the same server maps and entry dialects as
`nexus config import-mcp` (`mcpServers`, `servers`, `mcp`, `context_servers`) and
is deliberately strict because a repository may be committed by someone else:

- It must be a UTF-8 JSON object in a regular file of at most 256 KiB. A symlink
  is refused. Duplicate keys, `NaN`/`Infinity`, comments and trailing commas are
  not accepted.
- Unknown top-level keys produce a warning. A wrapper that is not an object, or
  a server name defined in more than one wrapper, makes the whole file invalid,
  and nothing in it loads.
- An unusable entry (unknown key, bad timeout, missing command and URL, and so
  on) is reported as `invalid` for that server only; the other entries still
  load. Server names use letters, digits, `.`, `_` and `-`, up to 32 characters.
- A project entry may use `${workspaceFolder}` and `${workspaceFolderBasename}`;
  a relative `cwd` is resolved against the project path, and an omitted `cwd`
  defaults to the project path.
- A project name that is already an **enabled** global server name is invalid.
  A repository cannot silently replace a trusted global server; rename the
  project entry instead. A disabled global entry (including the offered
  `browser-use` default) reserves nothing, so a project may use that name, and
  that enabled project entry is the one a session starts. If the global entry is
  enabled later, the project entry becomes invalid at the next read (see
  [Changes and invalidation](#changes-and-invalidation)).

Each project has its own manager, connections and stderr logs. The same name in
two projects is isolated: starting it in one does not start or reuse it in the
other. A session sees the global scope plus its own project's enabled entries
only; a child inherits the servers its parent's session started with.

The file is read when that project's MCP state is used (an API read, session
creation or planning, a search or call, or a session snapshot), not by a file
watcher. A change is therefore noticed at the next use.

## Trust and tool permissions

Loading a project server does not grant its tools. Once connected, every call
still follows the session's permission setting, the
`mcp__<server>__<tool>` rules and an approval prompt when the session asks.
Only an explicit `readOnlyHint: true` tool runs without asking in an `ask`
session. This separates "this command may start" (no prompt) from "this tool may
change something" (the normal tool permission).

The daemon never writes an approval for a project server, and an
`~/.nexus/mcp-approvals.json` left by an earlier build is ignored. Deleting a
project closes its connections and forgets its state; nothing else is needed.

## Changes and invalidation

There is no approval record to inherit; the current file is always the
definition:

- **Changed entry.** Editing the command, an argument, an environment value,
  `cwd`, URL, header, tool filter, timeout or enabled flag closes the old
  connection at the next read. The next session starts the new definition.
  Reverting the file closes the changed connection again and starts the reverted
  one in a new session.
- **Removed or unusable entry.** Removing the server or making its entry invalid
  closes its connection at the next read. A disabled entry is kept in the
  project inventory but is never started.
- **Whole-file errors.** A duplicate server name, a non-object wrapper or an
  unreadable/oversized file invalidates the file, and every project connection
  is closed.
- **Collision with an enabled global name.** The project entry becomes
  `invalid` and its connection is closed; the global server is untouched. This
  also covers enabling a global entry after the project entry was running: the
  global server takes the name, and the project entry cannot return until the
  global entry is disabled or renamed. A disabled global entry does not collide.
- **Project identity.** A project id whose stored path no longer matches the
  current project path closes its connections, and deleting the project forgets
  its state.

A running session keeps the server list frozen at its start, but a changed or
removed project connection is closed. Searches and calls for that server then
fail with a fenced error until a new session starts with the current
configuration.

Reading a changed file publishes `mcp.changed` with its `file`.

## Lifecycle

- **Online before a session starts.** Creating a session (`POST /api/session`)
  first connects every enabled global server the session does not switch off,
  and every enabled, valid project server, in parallel, each within its
  connect timeout. Disabled and invalid project entries are not attempted. A
  server that cannot connect does not block the session: it is listed as
  `unavailable` in the session's `mcp` rows, is switched off for that session
  (`mcp:<server>` in its `disabled_tools`) and `mcp.failed` is published.
  Children inherit their parent's servers. A handoff continuation is a new root
  session, so it reloads both scopes and connects them the same way; it keeps
  explicit switches but drops the automatic `mcp:<server>` entries above, so a
  server that has since been fixed is retried instead of staying off forever.
- **Global edits apply to new sessions.** The manager compares the file's
  content (not just its mtime and size) and applies a change to connections
  (new servers appear, changed or removed ones close). This also notices a
  rewrite whose timestamp and length happen to match the previous file. Running
  sessions keep what they started with; `mcp.changed` lets clients say
  "available in new sessions".
- **Project edits apply to new sessions.** A changed, removed or invalid
  `.mcp.json` entry closes its old connection at the next read, as described
  above. A new session starts the current definition; a running session keeps
  its frozen list. Project servers never fall back to an old command.
- **A dead server never fails a turn.** A call to it returns an error result and
  `mcp.failed` is published once. Health is a `tools/list` round trip every 20 s
  (`ping` is not in newer protocol versions) and a transport error ends the
  connection at once. A failed server is not retried for 5, 15, 30, then 60 s
  unless the user restarts it (`POST /api/mcp/{name}/restart` for a global one,
  `POST /api/project/{id}/mcp/{name}/restart` for a project one). A
  new session, including a handoff continuation, retries after that delay.
- **stderr** of a global stdio server goes to `~/.nexus/logs/mcp/<server>.log`;
  a project server's goes under
  `~/.nexus/logs/mcp/projects/<16-hex-project-id>/<server>.log`. Names keep only
  letters, digits and `_`; a log over 1 MiB is truncated at the next start.
  stderr never enters context.

## What the model sees

Two global settings, in `config.toml`, applied when a session **starts** (never
to a running session). They cover global and enabled project servers together:

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

The listing is fenced untrusted data (see below), bounded at 120 000 characters
with any clipping stated. A session has the tools only if it started with at
least one server on; disabled or invalid project entries are never listed.

- `mcp_tool_search {query, server?, limit?}`: keyword search, or `select:server/tool,…`.
  It searches connected global and project servers; `server` limits it
  to one. `limit` is 1 to 10 (default 5). Each result shows `server/tool`,
  `read only` or `may change state`, the description and the **input schema
  (JSON)**: this is how the model gets a definition in the names modes.
  Searches live servers; auto-approved.
- `mcp_call {tool: "server/tool", arguments}`: calls one. A tool is read-only
  only when it declares `readOnlyHint: true`. Approval and permission rules are
  decided for the **target**: the rule key is `mcp__<server>__<tool>`, a
  read-only target runs without asking, and anything else follows the session's
  permission and the `[tools.permission]` rules. "Allow for this session" covers
  the exact `server/tool` target, not the normalized rule key, so two targets
  that normalize alike do not share a grant. Starting a project server does not
  approve its tools; each mutating call still asks as the session allows.

  The normalized key is lossy: punctuation becomes `_`, and a `__` inside
  either part is indistinguishable from the separator, so distinct targets can
  share `mcp__<server>__<tool>`. When more than one target known to the session
  has the same key, an `allow` rule for it is ignored: the call falls back to
  the session's `permission`, an exact-target session grant, or an approval
  prompt. `deny` and `ask` rules still apply to the shared key, and a rule for
  an unambiguous key behaves exactly as before.

**Switches.** `mcp:<server>` entries in `disabled_tools`, from the context header
(both clients), cover both scopes. Only servers the session started with can be
switched; a locked row (`unavailable`, or off at the start) needs a new session
(PATCH returns 400). A mid-session switch injects one JSON
notice, visible in the timeline, and is enforced by `mcp_tool_search` and
`mcp_call`: `{"mcp_unavailable": {"server": "x"}}` and
`{"mcp_available": {"server": "x"}}`.

The session JSON carries
`mcp: [{server, tools, state: on|off|unavailable, locked, error, scope: global|project}]`.
A `state: "disabled"` project entry is not part of a session and appears only in
the project API; an invalid project file appears there as a `(file)` `invalid`
row.

**Everything a server returns is untrusted data.** Descriptions, schemas,
results and error text are stripped of control, bidirectional and invisible
characters, bounded (24 000 characters per result; 8 000 for a schema in a
search) and wrapped in `<untrusted-mcp-data>` with a notice that it carries no
authority. Text that imitates the delimiters is replaced. The fenced text is
exactly what the model receives and what the timeline shows. Images and audio
are stored as artifacts ([api.md](api.md#artifacts-k8)): the text names the
artifact, and an image also reaches the model beside the text when it takes
images ([llm-providers.md](llm-providers.md#tool-result-media)).

## API and events

| Route | Result |
| --- | --- |
| `GET /api/mcp` | global scope: `{servers: [{server, state, error, tools, transport, enabled, …}]}`; `state` is `idle`, `connecting`, `connected`, `failed`, `disabled` or `invalid`. Never connects; shows no argument, env or header values |
| `GET /api/mcp/{name}` | the same plus `tool_names` (known once connected) and `log` |
| `POST /api/mcp/{name}/restart` | closes and reconnects; returns the row |
| `GET /api/project/{id}/mcp` | project scope: `{project_id, file, exists, revision, error, warnings, servers: [rows]}`. Never starts a server; reads the file and may close a changed or removed connection |
| `GET /api/project/{id}/mcp/{name}` | one project row plus `tool_names` and `log`; never starts a server |
| `POST /api/project/{id}/mcp/{name}/restart` | close and reconnect a project server |

A project row also has `source: "project"`, `project_id`, `file`, `warnings`,
and the non-secret facts from `config.describe()` (`transport`, `enabled`,
command basename and argument count or a credential-free URL, env/header names).
`state` is `idle`, `connecting`, `connected`, `failed`, `disabled` or `invalid`;
a file error is the special server `(file)` with `state: "invalid"`.

Events are global, live only. Project-scope MCP events also carry `project_id`
and `scope: "project"`:

- `mcp.connected {server, tools}`, `mcp.disconnected {server}`,
  `mcp.failed {server, error}`, `mcp.tools_changed {server, added, removed}`
  (informational: running sessions do not change).
- `mcp.changed`: `{}` when the global servers or the two `[mcp]` settings in
  `config.toml` change; `{file}` when a project file changes.

## Not yet

MCP resources and prompts, OAuth, a circuit breaker beyond the retry delays,
and Settings screens for adding or editing global servers. There is no project
approval screen or CLI command: an enabled project entry starts with the next
session. The old TUI/NSH project approval review is obsolete and must not be
used; it is being removed in a separate client change.
