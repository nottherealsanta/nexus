# MCP tool search plan (`McpSearch` / `McpCall`)

Status: in progress, 2026-10-04. Core runtime, settings, and Ratatui support are
implemented on branch `feat/mcp-search` in worktree `/private/tmp/nexus-mcp-search`.
The remaining work and verification gaps are recorded in sections 14–16. Where
this plan and the code disagree, the code wins, then `docs/`.

## 1. Problem

Every enabled MCP server's tools are bridged into `mcp__<server>__<tool>`
entries (`mcp/bridge.py`) and sent with their full JSON schemas on every
request (`ModelRequest.tools`). A few large servers can cost tens of thousands
of tokens per request before the user types anything, and most of those tools
are never used in a given session.

## 2. Goal

By default an MCP server's tools are **not** in context. The agent finds them
with `McpSearch` and runs them with `McpCall`. The user can opt a server back
into "load all tools" for one session, or always via Settings.

Non-goals: Anthropic-native deferred loading (`defer_loading`,
`tool_reference`, `tool_addition`). That was considered and rejected for now
(section 13); the design keeps it possible later inside
`model/providers/anthropic.py` only.

## 3. Behaviour summary

| Situation | Tools the model receives |
| --- | --- |
| No MCP servers enabled in the session | no `McpSearch` / `McpCall` |
| At least one enabled server is in **search** mode | `McpSearch` + `McpCall` (fixed schemas); search-mode servers' tools are absent |
| A server is in **all** mode | its `mcp__<server>__<tool>` entries, exactly as today |
| Every enabled server is in **all** mode | today's behaviour; no `McpSearch` / `McpCall` |

The **mode** of each server, in precedence order:

1. the session choice (`ContextMcpLoadingSelect`, section 7.1), if any;
2. `"tool_loading"` on the server entry in `mcp.json` (section 7.2);
3. the default, `"search"`.

The effective modes are frozen at the session's first turn, like the existing
skill/MCP/agent choices, because switching changes `ModelRequest.tools` and so
the prompt-cache prefix.

## 4. The two tools

Both live in bundle `mcp`, are built by `mcp/bridge.py` (no other module
imports the upstream `mcp` package), and have fixed schemas so the tools list
never changes during a session.

### 4.1 `McpSearch`

```json
{
  "queries": [
    {"server": "github", "query": "create issue"},
    {"server": "linear", "query": "select:list_issues,get_issue"},
    {"query": "screenshot"}
  ],
  "limit": 5
}
```

- `queries`: 1–8 entries. `server` is optional (omitted = every search-mode
  server visible to this agent). `query` is 1–256 chars.
- `select:a,b,c` returns exactly the named tools (server-local names or
  qualified `server/tool`); unknown names are reported per name.
- Any other query is a keyword search (section 5).
- `limit`: results per query, default 5, max 10.
- Read-only (`mutates = False`), `concurrency = "parallel"`.
- Only searches servers that are enabled, in search mode, and permitted for the
  calling agent (section 8). Tools of all-mode servers are already in context
  and are not returned.

**Result** (one section per query, in order; all text sanitised and wrapped in
`<untrusted-mcp-data>`):

```
Query 1 · server github · "create issue" · 3 of 41 tools
1. github/create_issue  (changes state)
   Description: …
   Input schema: {…}
2. …
Query 2 · server linear · select:list_issues,get_issue
   …
   not found: get_issue (closest: get_issue_by_id)
Query 3 · all servers · "screenshot" · server playwright failed: timed out (other servers searched)
```

Bounds: 8,000 chars per schema (clipping announced, with "call McpSearch with
select:<name> to see this schema alone" — a single-tool select gets 24,000), total result
under `tools.max_result_tokens`. The result carries a `context_note` (names
only) so compaction's `evict_tool_results` can replace it cleanly.

Each query fails on its own: an unknown server, a server that is off, in
all mode or failing returns an error line for that query only.

### 4.2 `McpCall`

```json
{"tool": "github/create_issue", "arguments": {"title": "…", "body": "…"}}
```

- `tool` accepts `server/tool` or the qualified `mcp__server__tool`.
- Resolved at **prepare** time against the current MCP snapshot:
  - unknown or removed tool → error "not found; search again", with up to 3
    closest names;
  - server off for this session → the same refusal `ReadMcpResource` gives;
  - server in all mode → error "this tool is loaded directly; call
    `mcp__server__tool`";
  - not permitted for this agent → error naming the restriction.
- `arguments` validated with `validate_tool_input` against the real schema; a
  failure returns the errors **plus the schema**, so the model can correct itself
  without another search.
- Permission key = the target's key (`mcp__server__tool`), so existing rules,
  wildcards (`mcp__github__*`) and "always allow" answers apply unchanged.
- Whether the call changes anything comes from the target, not the proxy (section 6).
- Timeout = the target server's `call_timeout_s`.
- Execution delegates to the bridged tool's `run`; the result is exactly what a
  direct call would return (same untrusted wrapping and bounds).

## 5. Search ranking (`nexus/mcp/search.py`, new, pure)

- Index per server, built from the cached tool list: tokens from the tool name
  (split on `_`, `-`, `.`, camelCase), parameter names, and description.
- Score: exact name match ≫ name-token match > parameter-name match >
  description match; a small prefix bonus; IDF weighting across the server set so
  "get"/"list" don't dominate.
- Deterministic order: score desc, then qualified name. Same input + same
  catalogue = same output (replay and test stability).
- No embeddings, no network, no model calls. Bounded: ≤ 2,000 tools indexed per
  server (more is reported, not silently dropped), ≤ 64 servers.
- Cache the index by (server, list-cache key) so repeated searches are cheap.

## 6. Tool manager change: decide "changes anything" per call

`ToolSpec.mutates` is fixed per tool, but a proxy's real value depends on its target.

- Add an optional seam on the registered tool: `resolve(arguments) ->
  ResolvedTarget | ToolError` returning the effective `ToolSpec` (name for
  display, `mutates`, `permission_key`, `timeout_s`, input schema) and its `run`.
- `ToolManager._prepare_one` calls it when present and uses the effective spec
  for validation, the approval policy, profile filtering and exclusivity. The
  prepared call keeps both names: `tool = "McpCall"`, `target =
  "mcp__github__create_issue"`.
- `research` profile: `McpCall` stays in the catalogue but any target that changes
  something is refused at prepare with a clear reason. (Today the profile drops tools
  that change something; a proxy can't be dropped by its static flag.)
- `core/loop.py` is untouched; it only sees prepared calls (layering rule 1).

This is the only change in `tools/`; it gets its own tests before anything
else is built on it.

## 7. Configuration and controls

### 7.1 Per session

- New host command `ContextMcpLoadingSelect(session, server, mode: "search" |
  "all" | None)`; `None` = follow config.
- Stored as a session event `context.mcp_loading_selected {server, mode}`,
  rehydrated like `context.extension_selected` (`session/session.py`). Survives
  reconnect; replayable.
- Locked after the first turn with the existing message ("locked after the first
  turn to preserve the prompt cache…").
- At the first `turn.started`, the runtime records
  `context.mcp_loading_frozen {server: mode, …}` so a later `mcp.json` edit does
  not change an existing session's request prefix. A server **added** after the
  first turn uses the default (search). If `McpSearch` was not yet present, adding it
  changes the tools list once — the same cost a new server has today.

### 7.2 Persistent (`mcp.json`)

```jsonc
{
  "servers": {
    "filesystem": { "command": "…", "tool_loading": "all" },
    "github":     { "url": "…" }            // search (default)
  }
}
```

- New allowed key `tool_loading: "search" | "all"` in
  `parse_server_config` (`mcp/client.py`); anything else is an
  `MCPConfigError` (unknown values are errors, like unknown keys).
- Lives in whichever scope defines the server (project wins by name, as today).
- Changing it affects new sessions only (frozen per session, section 7.1).

### 7.3 Settings control

Settings → MCP is a file editor today. Add a structured row per server above
the editor: name, scope, status, tool count, **Tool loading: Search | Load all
(~N tokens)**.

- New host command `SettingsMcpLoadingSet(scope, server, mode)` that patches
  only that one key in the right `mcp.json`, keeping comments and formatting
  (minimal text edit of the JSONC object; refuse with a named error rather than
  reformat if the entry can't be located safely). Hash-checked
  (`expected_sha256`) like other `Settings*` writes; path-checked by
  `settings_scope.py`.
- The editor reloads after the write so the raw file and the row agree.

## 8. Agents and subagents

- Tool intersections (`parent ∩ role ∩ requested`, `docs/agents.md`) apply to
  MCP tools that are found by search too. An agent definition naming `mcp__github__*` or a
  specific `mcp__github__create_issue` keeps its meaning: `McpSearch` only
  returns, and `McpCall` only runs, targets inside the agent's allowed set.
- `McpSearch`/`McpCall` are included for a child when the child has at least one
  permitted search-mode MCP target.
- Children inherit the parent session's frozen modes.

## 9. Context and inspection

- `ModelRequest.tools`: search-mode servers' tools are absent; `McpSearch` and
  `McpCall` appear in the built-in group.
- `mcp_index` part (`context/parts.py`) per server: name, health, mode, tool
  count, server `instructions` (clipped), and for search-mode servers the
  **tool names only**, bounded (≈ 600 chars per server, clipping announced:
  "+37 more; use McpSearch"). Names are cheap and tell the agent what to look
  for. Raise `MCP_INDEX_MAX_CHARS` if the bound needs it; still untrusted-wrapped.
- `ContextInspect` result: each `mcp_servers` row adds `tool_loading` (effective),
  `tool_loading_source` (`session` | `config` | `default`), `tool_count`, and
  `schema_tokens` (what "load all" would add). Every deferred tool remains
  listable: the user can see the full catalogue even though the agent only sees
  what it searched for.
- Context header MCP block: `github(41 · search)`, `filesystem(6 · all)`;
  token figure counts only what is actually sent, with "~N tokens deferred" in
  grey.

## 10. Surfaces

Shared presentation first (`ui_support/tool_details.py`,
`ui_support/context_header.py`), then each client. The web client is being
deprecated: it must keep working (new row fields are ignored; `McpCall` renders
through the generic tool row), but gets no new controls unless asked.

| Surface | Change |
| --- | --- |
| `ui_support/tool_details.py` | `McpSearch`: one labelled block per query (server, query, match count) then a row per match (tool, changes-or-not, parameters with types/required, description); errors per query. `McpCall`: title `server · tool`, then the arguments as labelled rows; result as for a direct MCP call |
| Permission prompt (all clients) | names the target (`github · create_issue`), shows its arguments, notes "via McpCall" |
| Ratatui context dialog (`ui/ratatui/workflows.py`) | MCP list: existing On/Off toggle plus a mode label; Enter → details page with "Load all tools into context (~N tokens)" / "Find tools by search" actions; locked after the first turn |
| Ratatui Settings → MCP | structured server rows with the loading switch (section 7.3) |
| Desktop (GPUI, `rust/desktop`) | deferred; no desktop feature work or testing is part of this implementation |
| CLI `/mcp`, `nexus run` JSONL | `/mcp` lists mode per server; JSONL events carry `McpCall` with `target` |

## 11. Durability and replay

- Search results and calls are ordinary tool calls/results in the session
  record; replay needs no new reducer logic beyond the two `context.*` events.
- `view/reduce.py`: `McpCall` tool rows carry the resolved `target` so the view
  shows the real tool after reconnect (computed at prepare and stored in the
  tool-call record, not recomputed from the live snapshot).

## 12. Security

- Everything a server says remains untrusted: search results are sanitised,
  bounded and wrapped exactly like descriptions/results today.
- Permissions are evaluated on the target's key; the proxy never widens access.
- A server's descriptions now reach the agent only when searched, which reduces
  standing prompt-injection surface. Permissions are still the real boundary.
- Searching may connect a server lazily; that uses the existing connect deadlines,
  backoff and circuit breaker (`mcp/manager.py`). A search never spawns a
  disabled server.

## 13. Decisions to record (`docs/decisions.md`)

- **MCP tools are found by search by default; called through a proxy.** Keeps
  `ModelRequest.tools` fixed for a session (prompt cache), works for every
  provider, and the agent's view is in the durable record.
- **Not Anthropic `defer_loading` / `tool_addition`.** Anthropic-only and
  model-gated, beta with prior shape churn, and would need a second path plus
  new records. Possible later as a provider-only optimisation that maps native
  calls back to `McpCall` in the record.
- **Mode frozen at first turn.** Same reason as the existing lock.

## 14. Implementation phases

Implementation was done across phases rather than kept green between each
phase. The Python full-suite run recorded below predates the latest fixes; do
not treat that run as final verification.

1. **Per-call target resolution in the tool manager** (section 6) — implemented.
   Tests: `tests/test_tools_manager_resolve.py` — permission key, the "changes
   anything" flag and the research-profile refusal all follow the target;
   validation errors include the schema; duplicate/exclusivity handling unchanged.
2. **Search core** — `nexus/mcp/search.py` (section 5) — implemented.
   Tests: `tests/test_mcp_search_rank.py` — tokenisation, `select:`, ordering
   determinism, bounds, clipping messages.
3. **Config and session state** — `tool_loading` key; `ContextMcpLoadingSelect`;
   `context.mcp_loading_selected` / `context.mcp_loading_frozen`; precedence and
   lock — implemented. Tests: `tests/test_mcp_loading_config.py` and
   `tests/test_session_mcp_loading.py` cover config, precedence, selection,
   freezing, reconnect, replay, and the new-server default. The session test
   was added after the last broad test run and still needs a final run.
4. **Bridge + runtime wiring** — build `McpSearch`/`McpCall`; drop search-mode
   tools in `_selected_manifest`; include proxies only when needed; agent
   intersections. Tests: `tests/test_mcp_search_tools.py` against
   `tests/fixtures/mcp_server.py` (search → call round trip, multi-query,
   per-query failure, lazy connect, server off, all-mode refusal, hot reload
   removing a tool); a test that the request's tools are byte-identical across
   iterations after searches — implemented. Search → call, schema correction,
   target restrictions, wildcard selection, lazy connection, disabled/all
   refusal, fixed request schemas, and hot removal have coverage in
   `tests/test_mcp_search_tools.py`. Explicit coverage is still needed for
   per-query transport failure isolation, result/schema clipping, and child
   agents inheriting MCP modes and authority. The `claude_agent` SDK path has
   existing generic tool-bridge tests, but this change has not been exercised
   through that provider.
5. **Context** — `mcp_index` changes, `ContextInspect` fields, context-header
   chips — implemented. `tests/test_context_parts_mcp.py` covers bounded names
   and deferred-token display. The Ratatui context header and MCP details were
   captured; a focused `tests/test_context_header.py` addition is still absent.
6. **Settings** — `SettingsMcpLoadingSet` with comment-preserving patch — implemented.
   Tests: `tests/test_settings_mcp_loading.py` (comments kept, hash conflict,
   scope policing, refuse-on-ambiguous).
7. **Surfaces** — shared `tool_details.py` rows, permission prompt target naming,
   Ratatui context dialog + Settings rows, and replay target display — implemented.
   `tests/test_tool_details_mcp_search.py`, Ratatui workflow tests, and a new
   native PTY operation check cover these paths. Ratatui context/details/Settings
   screenshots were captured. The Settings screenshot exposed a long server
   row that was subsequently shortened; recapture is outstanding. GPUI and
   desktop tests are deliberately deferred per the user's instruction. The
   `/mcp` slash command has not been extended with loading mode.
8. **Docs** — `docs/extensions.md` (MCP section), `docs/tools.md` (bundle `mcp`,
   new tools, research profile note), `docs/context.md` (`mcp_index`),
   `docs/config.md` (`tool_loading`), `docs/surfaces.md` (controls),
   `docs/host.md` (two commands), `docs/decisions.md` (section 13),
   `docs/module-map.md` (`mcp/search.py`) — updates are present. Final doc
   consistency review remains.

### Verification already completed

- Rust Ratatui unit suite: 59 passed, 2 ignored; native TUI build succeeded.
- Offline native terminal capture: MCP context list, server details, Settings
  server rows, and loading menu captured. This is a TUI check, not desktop QA.
- Initial targeted Python groups passed (including 177 host/docs/UI/layering
  checks, 84 tool-manager checks, and 22 MCP integration checks). Targeted tests
  added since those runs are not all included in those counts.
- A full Python run reported 5,293 passed, 312 skipped, 4 deselected, and 23
  failed. It ran before several subsequent fixes. A narrower rerun after early
  fixes reported 264 passed and 5 failed; two failures in that rerun were the
  session-freeze assertions that were addressed afterward. Neither result is a
  final run.
- Comparing failures with base revision `b5af0b0` found existing unrelated
  failures in queue/interrupt behavior, Ratatui attachments/session markers,
  the session summary/schema expectation, and a model-picker fake. Do not
  attribute the entire full-suite failure list to this work; rerun the focused
  failures and compare with base as needed.
- No desktop application tests or validation should be run for this plan.

## 15. Open questions

1. **Changing mode after the first turn.** The plan locks it, matching the existing
   skill/MCP lock. Alternative: allow it with an explicit "this resets the prompt
   cache" confirmation. Worth it?
2. **Tool names in `mcp_index`.** Recommended (cheap and helps search a lot), but
   it is a standing cost per search-mode server. Keep, cap, or make it optional?
3. **`claude_agent` provider.** It re-exposes Nexus tools through its own SDK
   MCP server (`_claude_agent_worker.py`). Existing generic SDK tool tests pass
   in the earlier suite, but proxy handoff and MCP search/call behavior through
   that provider remain unverified.

## 16. Remaining work

Done 2026-10-04 (second pass):

1. Focused groups pass. Full offline suite: 5,306 passed, 312 skipped, 13 failed;
   all 13 fail identically on base `b5af0b0` (queue/interrupt, attachments,
   session markers/cards, session summary/schema, model picker, native PTY
   keyboard). Fixed on the way: `ContextMcpLoadingSelect` rejected servers not
   yet connected (now validates against the inspected `mcp_servers` rows);
   `test_session_mcp_loading` wrongly compared live events to the whole log.
2. Phase-4 gaps closed in `tests/test_mcp_search_bounds.py`: per-server connect
   failure isolated per query, schema clipping and select-alone cap, total result
   cap, target timeout and duplicate `McpCall` forms, child agent inherits frozen
   modes and fixed proxy schemas, and the proxy catalogue for all/search/off/no-
   server. `claude_agent`: the worker exposes the two proxy schemas unchanged
   (`tests/test_claude_agent_provider.py`); it never executes tools, so proxy
   handoff is the generic Nexus tool path. Child-agent *denial* of out-of-set
   targets is covered by `test_proxy_target_restrictions_search_and_call` at the
   manager level, not through a real child run.
3. Freezing in the attached stream is tested (`live == events tail`). Added later:
   a detached-turn freeze test (`test_detached_turn_freezes_once_before_first_model_request`)
   and a replay test for a denied target with no resolved `target`
   (`test_denied_target_survives_replay_without_resolved_target`).
4. Shared header: `tests/test_context_header.py`. `/mcp` opens the context dialog,
   which already shows mode per server in Ratatui; no separate CLI output exists,
   so nothing more was added.
5. Settings row recaptured: `artifacts/ratatui-parity/ratatui-mcp-settings.png`
   (fits; only the trailing status clips in the narrow pane). TUI only.
6. Docs: README, `docs/extending.md`, `docs/cli.md` updated to the search default.

Still open: child-agent denial through a real child run; the open questions in
section 15. No commit or merge has
been made.
