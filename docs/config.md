# Configuration

Core reads `$NEXUS10_HOME/config.toml` (normally `~/.nexus/config.toml`).
A missing file uses defaults. Invalid keys fall back independently and appear
in the API's `errors`; malformed TOML is never overwritten by a patch.

The initial inventory is `defaults.effort`, `defaults.agent`,
`defaults.permission` (`allow` or `ask`) and `tools.disabled`. New root sessions
apply these values; explicit creation fields override them, and existing
sessions keep their frozen configuration. Child sessions retain inheritance.
Other plan items add their own operational settings as those features land.

There is no default model ([daemon.md](daemon.md#model-selection)) and no model setting: a `defaults.model`
in the file is listed in `errors` as an unknown setting and ignored.

`[agents.models]` maps each child-capable agent name to a non-empty list of
unique `provider/model` refs, for example `scout = ["codex/example", "copilot/example"]`;
a ref naming an unknown provider is refused. A new child without an explicit
model picks one ref uniformly from those whose provider is connected; the
selected ref is stored on the child and does not change when config changes.
Refs of disconnected providers are skipped, never silently: the child's snapshot
records `model_pick` (`pool`, `eligible`, `chosen`, `skipped`, and a `note` when
nothing was eligible), shown in the session JSON's `snapshot` summary. With no
eligible ref, or no list, the agent file's `model` applies, else the parent's. `[agents.fallbacks]`
uses the same format for an ordered list of fallback refs. A new child's fallback
list is frozen at creation (as `model_fallbacks`, with disconnected refs under
`skipped`) and is ignored when an explicit model is supplied.
After retries, a failed request can switch to the next fallback only before any
response stream event; partial responses are never replayed automatically. The
switch and a notice in the model's context are visible, and the new model remains
the child's model. Both clients edit both lists on the Settings → Agents page.

## Settings layout

The manifest marks Providers and Agents as page sections (`page = true`): each is
one entry in Settings that opens its own page; Esc returns to the section list.

- **Providers** lists every provider from `GET /api/provider` with its state
  (connected, `using $KEY` when the key comes from the environment, the model
  count, unverified) and an action that follows its `methods`: Connect starts a
  device flow whose code and link show on the row, Add key opens a masked field,
  Disconnect warns when agent model lists name the provider. Refresh models
  re-reads the catalog.
- **Agents** has "Default agent for new sessions" (`defaults.agent`) and "Default
  effort" (`defaults.effort`), then one line per agent that can run as a
  subagent, summarising its models (`parent's model`, one ref, or `3 models ·
  2 fallbacks · 1 skipped`). Selecting one opens its page (Settings › Agents ›
  name): its Models, each with its real odds over connected providers, and its
  ordered Fallbacks.

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
