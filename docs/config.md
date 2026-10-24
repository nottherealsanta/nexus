# Configuration and on-disk state

## Shared daemon environment and limits

Each UDS client's `Hello` supplies its environment inward only (at most 1,024
variables and 256 KiB). The first load captures it for that project; later clients
do not overwrite it. `daemon reload` replaces the idle project runtime with the
caller's environment. Provider, MCP, shell and command-hook environments are
project-owned; `NEXUS_VOICE` is applied to the runtime's voice configuration.
`NEXUS_HOME`, `NEXUS_DEV` and HTTP listener options remain process settings.
The shared process starts in `nexus_home()`, independent of any project cwd.
Resource defaults are 16 child processes and 6 model streams per provider, with
a turn ceiling of 32. Set `[daemon] max_processes` and
`max_requests_per_provider` in the user config (integer 1–1024); project layers
cannot set these process-wide limits. Shell, grep, command hooks and current
worktree commands take process leases. Later worktree setup/land steps must use
the same pool when implemented.


`nexus/config/` (L0) loads layered TOML into frozen `msgspec` structs. Every
struct forbids unknown fields: **a typo is a hard error**, never a silently
ignored setting. Field defaults are the built-in layer.

## Files

| File | Owns |
| --- | --- |
| `config/schema.py` | `ConfigV2` and its sections (defaults live on the structs), URL/host validators |
| `config/layers.py` | layer reading, deep merge, v1 → v2 bridging, env overlays, `load_effective` |
| `config/paths.py` | `nexus_home`, state DB path, `project_key`, per-project dirs, `resolve_within` |
| `config/__init__.py` | `Config`: the effective config handed to managers (`.v2` is the sectioned view) |

## Layer order (low → high)

1. built-in defaults (struct defaults)
2. `~/.nexus/config.toml` (user)
3. `<workspace>/nexus.toml` (the exact workspace root; parents and Git roots never contribute)
4. `<workspace>/.nexus/nexus.toml` (legacy, read-only fallback)
5. `<workspace>/.agents/nexus.toml` (what Settings writes)
6. `NEXUS_*` environment
7. command-line flags
8. per-session overrides

Tables merge deeply, scalars replace, and the permission `allow`/`ask`/`deny`
lists **append** across layers. Flat v1 files (`executable`, `model`, `sandbox`,
`timeout_seconds`, `context_chars`, `*_file`) are bridged into v2 rather than
rejected: a v2 layer anywhere promotes the whole config to v2. New files should
start with `config_version = 2`.

**Environment overlay (v2):** `NEXUS_<SECTION>__<KEY>=value` with a double
underscore per nesting level (lower-cased), e.g. `NEXUS_PERMISSIONS__MODE=ask`.
`NEXUS_VOICE=off` disables voice. Legacy v1 names (`NEXUS_MODEL`, …) still map.

**Secrets are references, never values:** `${env:VAR}`
are opaque strings resolved by the adapter at request time. Logs, reprs and
errors redact them and any URL userinfo (`util.redact_secrets`).

## Sections (`ConfigV2`)

| Section | Key fields (defaults) |
| --- | --- |
| `[agent]` | `name` orchestrator, `profile` coding, `instructions_file` SOUL.md, `memory_file` MEMORY.md, `agents_file` AGENTS.md, `max_iterations` 0 (unlimited), `max_turn_seconds` 0 (unlimited), `sandbox` workspace-write |
| `[model]` | `default`, `fast`, `plan`, `fallback` list, `params` (`temperature`, `max_output_tokens`, `thinking_budget`) |
| `[models]` | `claude_code_main_enabled` false, `claude_code_subagent_backend` `worker`, `default`/`fast`/`plan`, `refresh_ttl_days` 7, `usage_refresh_seconds` 300 (60-3600), `catalogue_url` (models.dev), `offline` false, `tiers`, `tier_efforts`, `reasoning_efforts`, `fallback`, `subagent_usage` |
| `[providers.<name>]` | see [models.md](models.md#providers) |
| `[context]` | `max_tokens` (unset = model window, 180000 if unknown), `safety_margin_tokens` 4000, `compaction` hybrid, `compact_at_fraction` 0.85, `on_overflow` `handoff` (durable same-session context cutover, see [context.md](context.md); set `compact` to opt out and keep in-prompt compaction only; with no config or only v1 layers it stays `compact` (env overrides do not promote), and any v2 layer promotes to `handoff`), `handoff_keep_fraction` 0.25 (verbatim history kept after a handoff, as a fraction of the input budget, in (0, 1)), `handoff_brief` `deterministic` (or `summarized`: optional session-model recall, falls back to deterministic), `[context.limits]` optional per-part token caps `memory`, `agents_md`, `skills_index`, `environment`, `attachments` (all unset by default = uncapped; only the input budget bounds a part; a set cap truncates that part) |
| `[permissions]` | `mode` allow, `allow`/`ask`/`deny` lists, `write_roots` (`./`), `read_denyroots`, `on_unattended` deny |
| `[tools]` | `shell_yield_s` (default 120: how long a foreground `shell` run blocks before moving to the background), `shell_max_s` (default 3600: hard limit for any shell job), `shell_log_max_bytes` 512 MiB per job (`0` = unlimited), `shell_log_total_bytes` 2 GiB per project, `shell_log_retention_days` 7, `shell_max_auto_wakes` 10 (consecutive turns a background job's completion may start without a human message), `shell_max_persistent_per_session` 4 and `shell_max_persistent` 8 (running persistent jobs: `shell` with `background` and no `timeout_s`), `shell_orphan_s` 3600 (the daemon stops persistent jobs and exits after this long with no viewer and no turn; `0` = never); deprecated aliases `bash_timeout_s` 120, `bash_yield_s`, `bash_max_s` 3600, each used only while its `shell_*` key is unset; `grep_timeout_s` 5.0, `max_result_tokens` 25000, `max_parallel` 8, `[tools.web]` (local search on, `searxng_instances`, `allowed_origins`, fetch on, timeouts, `max_results` 5, `max_output_bytes` 512000) |
| `[ext]` | `enabled`, `watch_interval_ms` 500 (0 disables), `dirs` (`.agents/tools`, `.nexus/tools`, `~/.nexus/tools`), `quarantine` true, `max_file_bytes` 262144 |
| `[agents]` | `enabled`, `default_type` task, `max_depth` 3 (values above 1 behave as 1 because only the root session spawns; 0 disables spawning), `max_concurrent` 4, `max_fanout` 16, `max_tier` high (the ceiling; each role's own `tiers` narrow it), `token_budget`, `cost_budget`, `seed_roles`; `max_live` 8 (children held at once, 1-64). Subagents are foreground-only ([agents.md](agents.md#foreground-only)): the deprecated background keys `background`, `max_pending_reports`, `max_report_batch`, `max_report_chars`, `report_coalesce_ms`, `max_auto_wakes`, `max_followup_chars` and `oneshot_wait_seconds` still parse so existing config files load, and are ignored. All integers are range-checked and reject booleans. A workspace file may lower `max_live`, never raise it (`config/layers.py`) |
| `[hooks]` | `enabled` (hook declarations live in `hooks.toml`, not here) |
| `[mcp]` | `enabled`, `connect_timeout_s` 20, `restart_max` 5 (servers live in `mcp.json`) |
| `[session]` / `[sessions]` | `snapshot_every` 20 (`store` is a legacy key kept for old files; storage is always SQLite) / `auto_archive_idle_seconds` 3600 (idle time before a session is auto-archived; 0 disables, ≤ 315360000), `auto_archive_days` 2 (legacy: no longer a threshold; 0 still disables auto-archive, ≤ 3650), `auto_title` true (name new sessions with one small model call), `title_model` `openai/gpt-5-mini` (a concrete `provider/model`) |
| `[worktrees]` | `sessions` false (new sessions can opt into one owned worktree), `checkpoints` true (hidden per-turn Git refs for main/external roots), `checkpoint_keep` 50 (bounded retention setting), `base` `current` (main checkout branch), `max_worktrees` 16 (user config only; 1–256), `commit_max_files` 2000, `commit_max_file_mb` 50; `setup` unset (argv list, at most 64 items and 8192 total characters), `setup_shell` false (string setup through shell), `setup_timeout` 300 seconds (max 1800), `setup_on_fail` `fail` or `warn`, `setup_for_subagents` `copy_only`, `copy` and `share_dirs` empty, `copy_max_mb` 20, `submodules` `none` / `top` / `recursive`. Numeric values are bounded and reject booleans; project config cannot set `max_worktrees`. Setup commands are repository code and use the project trust prompt described in [security.md](security.md). |

| `[settings]` | `confirm_edits` false |
| `[telemetry]` | `log_level`, `log_file`, `redact` patterns |
| `[voice]` | `enabled` true (dictation is on by default but never records or loads the model by itself), `autoload` false, `model`, pinned `revision`, `device`, `max_seconds` 120, `auto_send` false, `unload_after_minutes` 0 |
| `[live]` | `enabled` true, `voice` default (`sol`; also arbor breeze cove ember juniper maple spruce vale), `max_minutes` 30 (1-240), `carry_turns` 12 (0 disables, <= 100), `pause_seconds` 120 (5-3600: a paused call ends after this long, see [live.md](live.md#pause-vs-stop)), `progress` true, `instructions` (path; empty = `<workspace>/.agents/live/instructions.md` or bundled), `log_raw` false (dev only: bounded redacted sideband log in `~/.nexus/logs/live/`). Editable in the native terminal under Settings → GPT Live (the model `gpt-live-1-codex` is pinned and shown read-only). Experimental, see [live.md](live.md) |
| `[manager]` | `enabled` **false**, `max_launches_per_turn` 3 (1-20), `max_live_managed` 8 (1-50), `auto_compaction` **true**, `idle_compaction_minutes` 55 (5-1440), `state_snapshot` **true**. Gates `ManagerOpen` and every operation on the workspace manager session; the caps bound its `session_launch`; `auto_compaction`/`idle_compaction_minutes` set the lazy idle compaction (memory note, then handoff) and `state_snapshot` the per-request `## Nexus State` block, independently ([manager.md](manager.md#automatic-compaction-and-machine-memory)). Env: `NEXUS_MANAGER__ENABLED`, `NEXUS_MANAGER__MAX_LAUNCHES_PER_TURN`, `NEXUS_MANAGER__MAX_LIVE_MANAGED`, `NEXUS_MANAGER__AUTO_COMPACTION`, `NEXUS_MANAGER__IDLE_COMPACTION_MINUTES`, `NEXUS_MANAGER__STATE_SNAPSHOT`. The model is not configurable: it is pinned to `codex/gpt-6-luna` |
| `[updates]` | `check` true |

The native session worktree preference has three machine-local values: `none`,
`ask` and `always` (stored in project state as `worktree-mode.json`). Without
that preference, the legacy `[worktrees] sessions = true` setting means
`always`; false defaults to `ask`. Unattended Ask sessions continue in their
current checkout because they cannot answer the prompt. Noninteractive callers
retain the legacy boolean selection API. See [sessions.md](sessions.md#session-worktree-choice).

`[permissions] mode` defaults to `allow` in the schema while `PermissionEngine`'s
own constructor default is `ask`; `examples/nexus.toml` sets `ask`. Set the mode
explicitly for any shared workspace.

Web-tool hosts (`[tools.web]`) must be public: localhost, private and
`.local/.internal/.lan/.home/.test` names are rejected at config load, and the
outbound transport repeats the address check at connect time
([security.md](security.md)).

## Claude Code worker keys

For `[providers.<name>]` with `kind = "claude-code"` (spellings `claude_code` and
`claudecode` also resolve). They configure the external worker
([agents.md](agents.md#external-claude-code-workers)); on any other kind they
are a `ConfigError`. Unknown values fail at load, not at launch.

| Key | Default | Meaning |
| --- | --- | --- |
| `executable` | `claude` | Claude Code binary (>= 2.1.296) |
| `timeout_seconds` | none | Deprecated and ignored: workers have no wall-clock deadline (still accepted so old configs load) |
| `worker_permission_mode` | `dontAsk` | `default`/`manual` (= manual), `acceptEdits`, `plan`, `dontAsk`, `auto` (Claude's own classifier answers approvals; explicit opt-in); `bypassPermissions` is rejected |
| `worker_tools` | unset | `--tools` built-ins; an empty list is rejected |
| `worker_allowed_tools` / `worker_disallowed_tools` | unset | `--allowedTools` / `--disallowedTools` rules (<= 256 entries) |
| `worker_setting_sources` | `""` | `--setting-sources` (`user`, `project`, `local`); empty loads none and adds `--disable-slash-commands` |
| `worker_mcp_config` | unset | Explicit MCP JSON object string (<= 64 KiB); unset means `--strict-mcp-config` with no servers |
| `worker_restricted` | true | `--restricted` (no Bash/WebFetch unless named in `worker_tools`; file tools confined to the workspace) |

`api_key`, `base_url`, `api`, `auth`, `command`, `args`, `env`, `inherit_env`,
`permission_policy` and `profile` are not accepted on these sections. The
worker's child environment is the fixed safe allowlist plus `CLAUDE_CONFIG_DIR`.
`[models] claude_code_main_enabled` (default false) only controls main-model
selection; it does not enable or disable explicit worker roles. Settings -> Models
has an `Allow Claude Code as a main model` toggle for it (`ClaudeCodeSettingsSet`)
that says main selection uses the Agent SDK and subagents are configured per role.
`[models] claude_code_subagent_backend` (`worker` default, or `sdk`) picks how
`claude-code/<model>` subagents run: the interactive worker with Claude's own tools,
or the Agent SDK inside a normal Nexus child with Nexus tools and permissions
([agents.md](agents.md#external-claude-code-workers)). Settings -> Models shows it as
`Claude Code subagents run via` with a note on what each backend mediates.

### `[models] usage_refresh_seconds`

How often the daemon re-reads every connected provider's plan usage in the background,
whether or not the subagent usage gate below is on and whether or not a client is looking.
Default `300` (5 minutes); a whole number from `60` to `3600`, anything else is a config
error. User config only: a project file that sets it is a config error. Settings → Providers
has a `Refresh plan usage` selector (`UsageRefreshSettingsSet`) that writes it; the running
poll reads it on its next 30 s tick, so no restart is needed. Opening the usage panel
(`Ctrl+U`, `/usage`) always fetches fresh numbers regardless of this interval.

### `[models.subagent_usage]`

Gates subagent launches on the provider's remaining usage quota. On by default.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Turn the gate off. User config only. |
| `five_hour_min_remaining` | `10` | Minimum percent (0-100) left in the 5-hour window. |
| `weekly_min_remaining` | `5` | Minimum percent (0-100) left in the weekly window. |
| `monthly_min_remaining` | `2` | Minimum percent (0-100) left in the monthly window. |
| `refresh_seconds` | `180` | Minimum gap between the refreshes the gate itself triggers before a subagent spawn (60-3600). User config only. The idle poll is `[models] usage_refresh_seconds`. |
| `providers.<name>` | none | Per-provider overrides of the three thresholds; unset ones inherit. |

A project file (`nexus.toml`, `.agents/nexus.toml`) may only raise a threshold, so
the higher of the user and project values wins, including per-provider overrides.
Setting `enabled` or `refresh_seconds` in a project file is a config error.

```toml
[models.subagent_usage]
enabled = true
five_hour_min_remaining = 10
weekly_min_remaining = 5
monthly_min_remaining = 2
refresh_seconds = 180

[models.subagent_usage.providers.github-copilot]
monthly_min_remaining = 10
```

## Antigravity worker keys

For `[providers.<name>]` with `kind = "antigravity"` (alias `agy`; a section
literally named `antigravity` or `agy` with no `kind` also resolves). It configures
the subagent-only external worker ([agents.md](agents.md#external-antigravity-workers)).
Minimal config:

```toml
[providers.antigravity]
kind = "antigravity"
worker_inherits_agy_settings = true # default; may be omitted
```

| Key | Default | Meaning |
| --- | --- | --- |
| `executable` | `agy` | agy binary (`agy` on `PATH`, or an absolute path) |
| `timeout_seconds` | none | Deprecated and ignored: no wall-clock deadline (still accepted so old configs load). agy always gets an unbounded `--print-timeout` (one year) |
| `worker_inherits_agy_settings` | `true` | agy applies its own user/global settings, MCP servers and plugins; Nexus `PathGuard` does not apply inside agy. Omission allows execution, including without a provider section; explicit `false` refuses every assignment. Only used by Antigravity |
| `worker_permission_mode` | `auto` | `auto`, `accept-edits` or `plan` (passed as `--mode`); anything else, including bypass, is refused. With `worker_dangerously_skip_permissions = true`, `auto` is sent as `accept-edits` |
| `worker_dangerously_skip_permissions` | `false` | **Opt-in, dangerous.** `true` appends `--dangerously-skip-permissions`, so agy auto-approves ALL its tool actions with no Nexus mediation, and allows an `always-proceed` init. **User-level only:** set it in `~/.nexus/config.toml`; a project `nexus.toml`, `.nexus/nexus.toml` or `.agents/nexus.toml` that mentions it fails to load. Must be a boolean; refused on other provider kinds. See [security.md](security.md#external-antigravity-worker) |

To opt in, in `~/.nexus/config.toml` (never in a repository):

```toml
config_version = 2
[providers.antigravity]
kind = "antigravity"
worker_dangerously_skip_permissions = true
```

The Claude `worker_tools`, `worker_allowed_tools`, `worker_disallowed_tools`,
`worker_setting_sources`, `worker_mcp_config` and `worker_restricted` keys, and
`api_key`, `base_url`, `api`, `auth`, `profile`, `command`, `args`, `env`,
`inherit_env` and `permission_policy`, are refused rather than silently ignored.
There is no model key: the model is always explicit, as `antigravity/<id>` on a
role, a `Task` call or a `[models.tiers]` entry, and must be an ID `agy models`
reports. The section cannot be the main, plan, fast, fallback or title model.

## On-disk layout

| Location | Holds |
| --- | --- |
| `~/.nexus/` (`$NEXUS_HOME`) | machine and user state root |
| `~/.nexus/backups/nexus-v<N>-<UTC>.db` | one verified copy of the session database made when schema 10 replaced an older one; `0600`, dir `0700`, never deleted automatically ([sessions.md](sessions.md)) |
| `~/.nexus/nexus.db` | shared SQLite state: every project's sessions ([sessions.md](sessions.md)); file `0600`, dir `0700` |
| `~/.nexus/credentials.json` | provider secrets and OAuth refresh records; plaintext, owner-only `0600`, parent `0700` |
| `~/.nexus/config.toml` | user config; Settings → Providers/Setup write `[providers.*]` and `[models].default` here |
| `~/.nexus/{agents,skills,tools,providers}/`, `mcp.json`, `hooks.toml`, `SOUL.md` | user-scope extensions |
| `~/.nexus/daemon/shared-v5-<install>.sock` (+ pid, lock, log, http discovery) | one daemon per home/install ([host.md](host.md)); falls back to a private short dir when the path is too long |
| `~/.nexus/locks/sessions/<project-hash>/` | per-session `flock` files |
| `~/.nexus/projects/<project-hash>/` | per-project machine state: cache, logs (incl. `logs/mcp/`), `stage/`, extension trash |
| `~/.nexus/cache/` | shared models.dev cache, `agy-models.json` (last good `agy models` list), `update-check.json` |
| `~/.nexus/models/voice/` | downloaded dictation model ([voice.md](voice.md)) |
| `~/.nexus/logs/chat-stderr.log` | native-library stderr from the `nexus chat` controller while the TUI owns the terminal (bounded, 1 MB) |
| `~/.nexus/dev/` | isolated home in dev mode ([devtools.md](devtools.md)) |
| `<workspace>/nexus.toml` | project config |
| `<workspace>/.agents/` | project extensions and Settings writes: `nexus.toml`, `agents/`, `skills/`, `tools/`, `providers/`, `hooks/`, `hooks.toml`, `mcp.json`, `SOUL.md` |
| `<workspace>/.nexus/` | legacy project dir: read-only, lower precedence; writes always go to `.agents/` |
| `<workspace>/{SOUL,MEMORY,AGENTS}.md` | prompt files loaded into context ([context.md](context.md)) |
| `$XDG_CONFIG_HOME/nexus/tui.json` | terminal preferences (theme, panels, context preview) |

`project_key` = first 16 hex chars of `sha256(resolved workspace path)`.
Legacy session and trash directories are **not** imported; export sessions
before switching storage if they must be kept.

## Other environment variables

`NEXUS_IMAGE_PROTOCOL` (`kitty`, `sixel`, `iterm2`, `halfblocks`; default `auto`)
forces the native client's image protocol and skips its startup terminal probe.
`NEXUS_COMPLETION_SOUNDS=off` (also `0` or `false`) disables the native client's
generated completion audio cue independently of `NEXUS_VOICE_SOUNDS` recording
cues. Audio playback requires `sounddevice` and a working output device.

`NEXUS_HOME` (state root), `NEXUS_DEV` (dev mode), `NEXUS_HTTP`, `NEXUS_HTTP_HOST`,
`NEXUS_HTTP_PORT`, `NEXUS_HTTP_TOKEN`, `NEXUS_HTTP_ORIGINS` (opt-in HTTP/SSE
surface, [host.md](host.md)), `NEXUS_NO_UPDATE_CHECK`, `NEXUS_VOICE=off`, `NEXUS_LIVE=off` (disables GPT Live calls).
Command hooks receive `NEXUS_HOOK_EVENT`, `NEXUS_TOOL_NAME`, `NEXUS_TOOL_KEY`,
`NEXUS_TOOL_PATH`, `NEXUS_TOOL_BUNDLE`, `NEXUS_SESSION_ID`, `NEXUS_TURN_ID`
([extensions.md](extensions.md#hooks-hooks)). Live tests use their own gate variables
(for example `NEXUS_COPILOT_THINKING_LIVE=1`).

## Changing config

1. Add the field to the section struct with a default and a `__post_init__`
   validation if it has constraints (bounded numbers, enumerations).
2. If it should be settable from Settings or `nexus doctor`, expose it through
   `host_support/settings_inventory.py` / `doctor.py`.
3. Document it here and in `examples/nexus.toml`.
4. Tests: `tests/test_config_*.py`.

For the native terminal client, `NEXUS_TUI_BINARY` explicitly selects
the Rust executable. Shell preferences retain `$XDG_CONFIG_HOME/nexus/tui.json`
(or `~/.config/nexus/tui.json`) and saved appearance and layout keys. Native startup
never opens session storage; session data continues to come from the host.

## MCP tool loading

Each server entry in `.agents/mcp.json` or `~/.nexus/mcp.json` accepts
`"tool_loading": "search" | "all"`. Omission means Search; other values are
configuration errors. Project definitions replace global definitions by name.
Session choices override config until modes freeze at the first turn. Settings
changes affect new sessions; see [extensions.md](extensions.md#mcp-search-loading).

## MCP server enablement and diagnostics

MCP config is JSONC: `//` and block comments and trailing commas are accepted.
Use a `servers`, `mcpServers`, `mcp` or `context_servers` object, not a top-level
array; entries in the VS Code, Claude, Cursor, OpenCode, Zed and Gemini formats
load as written ([extensions.md](extensions.md#mcp-config-dialects)). Each server
accepts `"enabled": true | false` or `"disabled": true | false` (default on; strings are errors). Disabled
servers remain configured but do not connect or expose tools, resources, or
prompts. Settings → MCP → server → Switch On/Off patches just this field,
preserving comments and other values in the defining global/project file.
This is persistent configuration, distinct from session-only context toggles.

`nexus --workspace /path/to/project doctor` discovers configuration even before
the first turn. Both doctor (including `--json`) and `/mcp` display bounded,
redacted configuration diagnostics, including the source path and parsing or
validation error even when there are no live servers. A malformed file retains
its own previous definitions (the other file still loads) and appears as an
invalid row in Settings and the context header; fix the reported file rather than assuming an empty
server count means the file was ignored. Global `~/.nexus/mcp.json` definitions
are merged with the selected workspace's `.agents/mcp.json` by name (project
wins); `.nexus/mcp.json` is only a project fallback when `.agents/mcp.json` is absent.

## Subagent tiers and effort

Tiers are for subagents only. `[models.tiers]` lists provider/model IDs in fallback
order. Optional `[models.tier_efforts.low]` (and other tier names) maps those IDs
to supported effort strings, e.g. `"openai/gpt-5-mini" = "high"`. Settings edits
these per-entry selections; Default removes an override. Unsupported efforts
are refused by the host. Legacy `agents.max_tier` remains parseable but is no
longer a runtime ceiling or a Settings control. Role restrictions and nested
parent tier bounds still apply. Session titles default to `openai/gpt-5-mini`;
legacy tier-valued title settings keep the first-message title until changed.

Antigravity `worker_permission_mode` defaults to `auto`; explicit `accept-edits`
or `plan` remain supported. The mode does not override an explicit inheritance opt-out,
tool restriction checks, or the ban on bypass/always-proceed flags unless the
user-level `worker_dangerously_skip_permissions` opt-in is set (then `auto` is sent as `accept-edits`).

### Code mode

On by default. `pydantic-monty==1.1.0` is a base dependency, so no extra install is
needed (`nexus[codemode]` remains as an alias). If the sandbox package is missing or
the wrong version the `code` tool is simply not offered and `nexus doctor` says why.
Set `enabled = false` to turn it off. Defaults shown:

```toml
[code_mode]
enabled = true
expose = "read_only" # or "all", or an explicit list of tool/glob names
context = false
max_workers = 2
max_tool_seconds = 120
max_nested_calls = 64
inline_budget_tokens = 2500
pinned = []
# sub_model = "..."  # model for llm(); unset = the session's own model
max_sub_calls = 16
max_sub_input_tokens = 32000
max_total_sub_tokens = 200000
max_depth = 1
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Advertise the `code` tool (needs the Monty dependency; absent means the tool is not offered). |
| `expose` | `"read_only"` | Callable tools: `"read_only"`, `"all"`, or a list of names / `mcp__server__*` globs. |
| `context` | `false` | Bind the `context_*` functions and `llm()`. |
| `max_workers` | `2` | Sandbox worker processes (1-16). |
| `max_tool_seconds` | `120` | Upper bound for one program's `timeout_s` (1-3600). |
| `max_nested_calls` | `64` | Tool calls one program may make (1-10000). |
| `inline_budget_tokens` | `2500` | Estimated tokens of stubs inlined in the `code` description; the rest is found with `search_tools()` / `describe_tool()`. |
| `pinned` | `[]` | Tool names whose stubs are listed first in the inline catalog. |
| `sub_model` | unset | Model used by `llm()`; unset uses the session's model. |
| `max_sub_calls` | `16` | `llm()` calls per program (1-1000). |
| `max_sub_input_tokens` | `32000` | Input tokens per `llm()` call. |
| `max_total_sub_tokens` | `200000` | Input plus output tokens across a program's `llm()` calls. |
| `max_depth` | `1` | Nesting depth of sub-model calls (1-8). |

The manager profile never gains `code`. Disabled sessions and installations
without the exact optional dependency do not advertise it. Exposure intersects
the selected tool catalog and excludes loop-driving tools.
