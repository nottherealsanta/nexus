# Configuration

Core reads `$NEXUS10_HOME/config.toml` (normally `~/.nexus10/config.toml`).
A missing file uses defaults. Invalid keys fall back independently and appear
in the API's `errors`; malformed TOML is never overwritten by a patch.

The initial inventory is `defaults.effort`, `defaults.agent`,
`defaults.permission` (`allow` or `ask`) and `tools.disabled`. New root sessions
apply these values; explicit creation fields override them, and existing
sessions keep their frozen configuration. Child sessions retain inheritance.
`defaults.model` is deprecated and is not edited in Settings: with no model ever selected, a value there
(or `$NEXUS_MODEL`) is the starting model of a new session and of a session stored before models were
remembered; the first model the user selects replaces it as the new-session starting point.
Other plan items add their own operational settings as those features land.

`[agents.models]` maps each child-capable agent name to a non-empty list of
unique `provider/model` refs, for example `scout = ["codex/example", "copilot/example"]`.
A new child without an explicit model samples its agent's list uniformly once;
the selected ref is stored on the child and does not change when config changes.
An omitted agent retains its agent-file or daemon default. `[agents.fallbacks]`
uses the same format for an ordered list of fallback refs. A new child's fallback
list is frozen at creation and is ignored when an explicit model is supplied.
After retries, a failed request can switch to the next fallback only before any
response stream event; partial responses are never replayed automatically. The
switch and a notice in the model's context are visible, and the new model remains
the child's model. NSH Settings edits both lists for the current project's agents.

`GET /api/config` returns `{revision, values, defaults, schema, errors}`.
`PATCH /api/config` accepts `{expected_revision, set: {"defaults.effort":
"high"}, unset: []}`. Stale revisions return 409. Daemon patches are
serialized, preserve comments, and replace the file atomically using a private
temporary file. `config.changed` announces writes and outside edits detected
by the daemon's two-second poll. Both clients reload Settings on that event.
Shared `ui.detail` (`low`, `medium`, `high`) and `ui.reduced_motion` are
read and written by both clients. Client layout preferences remain local.

`[tools.permission]` holds permission rules, `"bash(git push*)" = "deny"`; see
[tools.md](tools.md#permission-rules). Rules that do not parse are listed in `errors`
and skipped, and the others still apply.

`[web]` configures the web tools: `search_url` (default
`http://127.0.0.1:18765/search`, the `nexus-search/` SearXNG; any http(s)
SearXNG `/search` endpoint works) and `fetch_timeout_s` (default 15, 1 to 60;
a call may ask for less, never more than 20). The web tools read these on each
call. Neither client has a Settings row for them yet.

`nexus config import-mcp <path>` imports `servers`, `mcpServers`, `mcp` and
`context_servers` maps into `[mcp.servers.<name>]`, refusing duplicate names.
Entries retain their source dialect and are read by the MCP manager ([mcp.md](mcp.md)); `browser-use` is listed switched off when the file does not mention it. The config API
exposes these entries with environment values, headers and credential fields
masked. PATCH of `mcp.servers` restores unchanged `********` placeholders from
the existing file. Importing configuration does not start an MCP server.

Provider credentials remain in the auth store. `POST /api/provider/{id}/login`
starts asynchronous device sign-in; `GET` on that path reports status and
`provider.login` events carry instructions and completion. OpenCode accepts
`{api_key}`. `DELETE` cancels a pending flow and signs out. Credentials never
appear in login events.

## `[context]` settings

`handoff_percent` (integer 0 to 100, default 80): after a completed turn leaves a root session at least this full,
Core continues in a new session from a handoff summary (docs/api.md, "Handoff, titles and `/btw`"). 0 turns it off.

`title_models` (list of `provider/model`, default `copilot/gpt-6-luna`, then `codex/gpt-6-luna`): tried in order to
write a session title. If none answers (each gets 20 s) the first words of the first message stay as the title.

## `[mcp]` settings

`full_definitions` (boolean, default false) and `max_listed_tools` (integer 1 to 1000, default
25) choose how a **new** session lists MCP tools in its context; running sessions keep theirs
([mcp.md](mcp.md#what-the-model-sees)). Servers are `[mcp.servers.<name>]` tables.
