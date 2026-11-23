# MCP

MCP servers come from two scopes, and the model reaches both through the same
two proxy tools (`mcp_tool_search`, `mcp_call`). Code:
`nexus-core/nexus/mcp/`, using the `mcp` SDK.

- **Global** servers are the user's configuration in `~/.nexus/config.toml`
  as `[mcp.servers.<name>]`. They need no approval.
- **Project** servers are `<project>/.mcp.json` and `<project>/.agents/mcp.json`.
  They are trusted configuration: every enabled, valid entry starts with a new
  session in that project, with no approval prompt.

> **Warning: the project MCP files are arbitrary code.** Opening a project does
> not run them, but starting a session in that project starts each enabled server
> they name (a stdio `command` and its arguments), with the user's permissions and
> no sandbox. A committed file may come from someone else, and a project
> directory is not a security boundary. Read the files before starting a session
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
| OAuth | `oauth`: `true`, `false`, or a table `{scope, client_id, client_secret, redirect_port}` (remote only; see [Remote servers and OAuth](#remote-servers-and-oauth)). Unset: on for a remote entry that sends no `Authorization` header |
| Shown, not acted on | `autoApprove`, `alwaysAllow`, `trust`, `auth`, `description`, `version`, `tool_loading`, `alwaysLoad`, … (loading is the global setting below; a tool call still follows the permission rules) |

`${env:VAR}`, `${VAR}`, `${VAR:-default}`, `{env:VAR}`, `${userHome}`,
`${pathSeparator}` and `${/}` are replaced in `command`, `args`, `env`, `url`,
`headers` and `cwd`; a missing variable without a default is an error naming it.
`${workspaceFolder}` and `${workspaceFolderBasename}` are refused in **global**
entries (servers are shared by every project) and allowed in **project**
entries. `${input:…}` is refused everywhere. Any other key is an error for that
server only; the others still load. `GET /api/config` masks global `env` and
`headers`, and a PATCH that sends the mask back keeps the stored value.

No server is offered by default. browser-use, offered here switched off until 2026-11-13, is now a
Nexus tool: `browser_exec` and `browser_screenshot` ([tools.md](tools.md#browser-tools)). An existing
`[mcp.servers.browser-use]` entry that only said `enabled = true` has no command any more and shows
as invalid. Delete it, or give it a full entry if you still want browser-use as an MCP server too.

## Managing servers

Servers are added, changed and removed through Core, which writes the file for you and applies the change at once
(the next session uses it; running sessions keep theirs). Every route is checked like a file edit: the entry is parsed
as it would be at start-up, so a typo is refused when it is saved. A `${env:NAME}` that is not set yet is accepted on
save, and the row then says which variable is missing.

- **Settings › MCP** (TUI and NSH, desktop and web only): *Add a remote server* asks for the address and nothing else.
  Core names the server from its address (`https://mcp.linear.app/mcp` is `linear`; a number is added when the name is
  taken). *Add to this project* writes the project's `.mcp.json`. Each server opens a detail with **Sign in** (when it
  uses OAuth), **Add a header** (typed once as `Name: value`; only the name is shown afterwards), **Switch off or on**,
  **Restart** and **Remove**.
- **`nexus mcp`**: `list`, `add <name> <url> [--header 'Name: value'] [--transport http|sse] [--no-oauth] [--client-id …]
  [--project]`, `remove`, `restart`, `login`, `logout`. It talks to the running daemon, so a change and a sign-in are
  seen at once. `nexus config import-mcp` stays the way to bring in a whole file, including local (stdio) servers.
- **API** (below): `POST /api/mcp`, `PUT`/`PATCH`/`DELETE /api/mcp/{name}`, and the same under
  `/api/project/{id}/mcp`. A global write goes through the same revision-safe path as `PATCH /api/config`, keeps comments and
  other settings, and keeps a `********` secret as stored. A **project** write adds, replaces or removes one named
  server in the file that already defines it (else `mcpServers` in `.mcp.json`) and leaves every other key alone. A file the strict
  reader refuses (a symlink, comments, a trailing comma, duplicate keys, over 256 KiB) is never rewritten: fix it first.

> A project file is committed to a repository. Core refuses to write a literal credential there (a header, `env` or
> `oauth.client_secret` whose name looks like a secret): write `${env:NAME}` and set `NAME` in the environment Nexus
> runs in. The global table lives in `~/.nexus/config.toml`, which is private to you.

## Remote servers and OAuth

A remote entry (`url`) is a streamable-HTTP server, or SSE when the address ends `/sse`. It authenticates in one of two ways:

- **Headers**: `headers = { Authorization = "Bearer ${env:DOCS_TOKEN}" }`. An entry that sends its own `Authorization`
  header does not use OAuth.
- **OAuth** (the default for a remote entry without one): authorization code with PKCE, through the SDK's OAuth client
  (`mcp/oauth.py`). Core discovers the server's authorization server, registers Nexus as a client dynamically, and the user
  signs in once.

How a sign-in works:

1. A connection never opens a browser. Without a stored sign-in it only notes a `401`: the row shows `state: failed`,
   `auth: required` (the clients say "sign-in required"). With one, it sends the token and refreshes it when it expires.
2. The user starts the sign-in (`POST …/login`, Settings › Sign in, or `nexus mcp login`). Core listens on `127.0.0.1` on a
   free port for the redirect, publishes `mcp.login` with the sign-in link, and the client opens it. The daemon never launches a browser.
3. When the redirect arrives Core exchanges the code, stores the tokens, restarts the server and publishes `mcp.login`
   `signed_in`. Waiting ends after 5 minutes; `DELETE …/login` cancels.

Tokens and the registered client are in `<home>/auth.json` (owner-only, 0600), under `mcp-oauth:<scope>:<name>:<address hash>`, never in
`config.toml` and never in an API response. The address hash means editing a server's URL drops its old tokens instead of
sending them to the new address; removing a server forgets them. The same name in two projects, or a project and the
global table, has separate sign-ins.

A server with no dynamic registration needs a client registered by hand: `oauth = { client_id = "…", client_secret =
"${env:SECRET}", scope = "read", redirect_port = 18791 }`, and `http://127.0.0.1:<redirect_port>/callback` registered with the provider (the
port defaults to 18791 when `client_id` is set, else any free port).

## Project servers

Two files are read, at the project record's path: `<project>/.mcp.json` (the
MCP project-file convention) and `<project>/.agents/mcp.json` (Nexus's project
agent-config folder, alongside `.agents/agents`, `.agents/skills` and
`.agents/tools`). A nested file and the directory a session has moved to are not
read. Nexus does not merge either file with the global table. A worktree session
still uses its project's files, and a stdio entry's default working directory is
the project path (not the session's current directory).

Both files accept the same server maps and entry dialects as
`nexus config import-mcp` (`mcpServers`, `servers`, `mcp`, `context_servers`) and
are deliberately strict because a repository may be committed by someone else:

- Each file must be a UTF-8 JSON object in a regular file of at most 256 KiB. A symlink
  is refused. Duplicate keys, `NaN`/`Infinity`, comments and trailing commas are
  not accepted.
- Unknown top-level keys produce a warning naming the file. A wrapper that is not an object, or
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
  project entry instead. A disabled global entry reserves nothing, so a project may use that name, and
  that enabled project entry is the one a session starts. If the global entry is
  enabled later, the project entry becomes invalid at the next read (see
  [Changes and invalidation](#changes-and-invalidation)).
- A name defined in both files is a duplicate. `<project>/.mcp.json` wins, and
  the ignored `.agents/mcp.json` definition is reported as a warning in
  `GET /api/project/{id}/mcp` (top-level `duplicates`, and `warnings`/`duplicate`
  on the server row). The clients show it when the MCP details are opened. A
  file-level error applies only to that file; the other file's servers still load.

Each project has its own manager, connections and stderr logs. The same name in
two projects is isolated: starting it in one does not start or reuse it in the
other. A session sees the global scope plus its own project's enabled entries
only; a child inherits the servers its parent's session started with.

The files are read when that project's MCP state is used (an API read, session
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
  project-file entry closes its old connection at the next read, as described
  above. A new session starts the current definition; a running session keeps
  its frozen list. Project servers never fall back to an old command.
- **A dead server never fails a turn.** A call to it returns an error result and
  `mcp.failed` is published once. Health is a `tools/list` round trip every 20 s
  (`ping` is not in newer protocol versions) and a transport error ends the
  connection at once. A failed server is not retried for 5, 15, 30, then 60 s
  unless the user restarts it (`POST /api/mcp/{name}/restart` for a global one,
  `POST /api/project/{id}/mcp/{name}/restart` for a project one). A
  new session, including a handoff continuation, retries after that delay.
- The first connection loads the SDK and its schema validator off the Core event loop.
  Transport setup also runs off the loop; Core rechecks the configuration before opening the transport.
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
| `POST /api/mcp` | add a global server: `{entry, name?}` → 201 with its row (`name` is made from the address when omitted; 409 if taken, 400 if the entry does not parse). A new remote server connects at once, so the row already says `connected` or `auth: required` |
| `PUT /api/mcp/{name}` | replace the entry (`{entry}`); a `********` keeps the stored secret |
| `PATCH /api/mcp/{name}` | merge into the stored entry: `{entry: {headers: {Authorization: "Bearer …"}, enabled: false}}`; a `null` removes a key (one level deep inside a table) |
| `DELETE /api/mcp/{name}` | remove it and forget its sign-in; `{server, removed: true}` |
| `POST`/`GET`/`DELETE /api/mcp/{name}/login` | start (202 `{status: "pending", verification_uri?}`), read, or cancel and sign out (204) an OAuth sign-in. 400 for a server that does not use OAuth |
| `GET /api/project/{id}/mcp` | project scope: `{project_id, file, exists, revision, error, warnings, servers: [rows], files, duplicates}`. Never starts a server; reads the files and may close a changed or removed connection |
| `GET /api/project/{id}/mcp/{name}` | one project row plus `tool_names` and `log`; never starts a server |
| `POST /api/project/{id}/mcp/{name}/restart` | close and reconnect a project server |
| `GET /api/mcp/{name}/resources` and `/prompts` | `{server, resources: [{uri, name, description, mime_type}]}` or `{server, prompts: [{name, description, arguments: [{name, required, description}]}]}`; at most 200, sanitized. Connects the server (404 if off, invalid or failing) |
| `GET /api/mcp/{name}/resource?uri=` | `{server, uri, text}`: the resource text fenced as `<untrusted-mcp-data>` (`kind: resource`), clipped at 24,000 characters; binary contents are named, never shown |
| `GET /api/mcp/{name}/prompt?name=&arguments=` | `{server, prompt, text}` with `arguments` a JSON object of strings; the prompt's messages fenced (`kind: prompt`). Nothing is sent to a session |
| `/api/project/{id}/mcp/{name}/resources`, `/resource`, `/prompts`, `/prompt` | the same for a project server |
| `POST /api/project/{id}/mcp`, `PUT`/`PATCH`/`DELETE …/{name}`, `…/{name}/login` | the same writes and sign-in for a project server; the file edit is described in [Managing servers](#managing-servers) |

A remote row also has `oauth: true|false` and, when OAuth is on, `auth: "signed_in" | "required" | "none"`
(`required`: the server answered 401; `none`: no sign-in stored yet).

A project row also has `source: "project"`, `project_id`, `file` (the winning
file), `warnings`, and the non-secret facts from `config.describe()`
(`transport`, `enabled`, command basename and argument count or a
credential-free URL, env/header names). A duplicated name also carries
`duplicate: {server, used, ignored}`. `state` is `idle`, `connecting`,
`connected`, `failed`, `disabled` or `invalid`; a file error is the special
server `(file)` with `state: "invalid"`.

Events are global, live only. Project-scope MCP events also carry `project_id`
and `scope: "project"`:

- `mcp.connected {server, tools}`, `mcp.disconnected {server}`,
  `mcp.failed {server, error}`, `mcp.tools_changed {server, added, removed}`
  (informational: running sessions do not change).
- `mcp.changed`: `{}` when the global servers or the two `[mcp]` settings in
  `config.toml` change; `{file}` when a project file changes.
- `mcp.login`: `{server, status: pending|signed_in|failed|signed_out, message?, verification_uri?}` (project scope adds
  `project_id` and `scope`): a sign-in's link, then its end. Global, live only; carries no token.

## Not yet

The resources and prompts routes above are Core-only: there is no model tool for them (the model still
reaches a server through `mcp_tool_search` and `mcp_call`) and no client UI yet. Also missing: a circuit breaker beyond the
retry delays; adding a local (stdio) server from Settings (`nexus config import-mcp` or the file does it); editing an
existing entry's address or tool filters from Settings (remove and add it again, or `PUT`); refreshing an expired OAuth
token for a server whose provider issues no refresh token (the row asks for a new sign-in); and OAuth
client credentials other than a pre-registered `client_id`/`client_secret` (no `private_key_jwt`). There is no project
approval screen or CLI command: an enabled project entry starts with the next
session. The old TUI/NSH project approval review is gone.
Mobile has no MCP page: it is a desktop and web surface ([AGENTS.md](../AGENTS.md)).
