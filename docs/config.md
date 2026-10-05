# Configuration and on-disk state

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
| `[agent]` | `name` build, `profile` coding, `instructions_file` SOUL.md, `memory_file` MEMORY.md, `agents_file` AGENTS.md, `max_iterations` 0 (unlimited), `max_turn_seconds` 1800, `sandbox` workspace-write |
| `[model]` | `default`, `fast`, `plan`, `fallback` list, `params` (`temperature`, `max_output_tokens`, `thinking_budget`) |
| `[models]` | `default`/`fast`/`plan`, `refresh_ttl_days` 7, `catalogue_url` (models.dev), `offline` false, `tiers`, `reasoning_efforts`, `fallback` |
| `[providers.<name>]` | see [models.md](models.md#providers) |
| `[context]` | `max_tokens` (unset = model window, 180000 if unknown), `safety_margin_tokens` 4000, `compaction` hybrid, `compact_at_fraction` 0.85, `[context.limits]` memory 8000, agents_md 8000, skills_index 4000, environment 2000, attachments 20000 |
| `[permissions]` | `mode` allow, `allow`/`ask`/`deny` lists, `write_roots` (`./`), `read_denyroots`, `on_unattended` deny |
| `[tools]` | `bash_timeout_s` 120, `bash_yield_s`, `bash_max_s` 3600, `grep_timeout_s` 5.0, `max_result_tokens` 25000, `max_parallel` 8, `[tools.web]` (local search on, `searxng_instances`, `allowed_origins`, fetch on, timeouts, `max_results` 5, `max_output_bytes` 512000) |
| `[ext]` | `enabled`, `watch_interval_ms` 500 (0 disables), `dirs` (`.agents/tools`, `.nexus/tools`, `~/.nexus/tools`), `quarantine` true, `max_file_bytes` 262144 |
| `[agents]` | `enabled`, `default_type` task, `max_depth` 3, `max_concurrent` 4, `max_fanout` 16, `max_tier` high (the ceiling; each role's own `tiers` narrow it), `token_budget`, `cost_budget`, `seed_roles` |
| `[hooks]` | `enabled` (hook declarations live in `hooks.toml`, not here) |
| `[mcp]` | `enabled`, `connect_timeout_s` 20, `restart_max` 5 (servers live in `mcp.json`) |
| `[session]` / `[sessions]` | `snapshot_every` 20 (`store` is a legacy key kept for old files; storage is always SQLite) / `auto_archive_days` 2 (0 disables, ≤ 3650), `auto_title` true (name new sessions with one small model call), `title_model` `low` (a tier or `provider/model`) |
| `[settings]` | `confirm_edits` false |
| `[telemetry]` | `log_level`, `log_file`, `redact` patterns |
| `[voice]` | `enabled`, `autoload` false, `model`, pinned `revision`, `device`, `max_seconds` 120, `auto_send` false, `unload_after_minutes` 0 |
| `[updates]` | `check` true |

`[permissions] mode` defaults to `allow` in the schema while `PermissionEngine`'s
own constructor default is `ask`; `examples/nexus.toml` sets `ask`. Set the mode
explicitly for any shared workspace.

Web-tool hosts (`[tools.web]`) must be public: localhost, private and
`.local/.internal/.lan/.home/.test` names are rejected at config load, and the
outbound transport repeats the address check at connect time
([security.md](security.md)).

## On-disk layout

| Location | Holds |
| --- | --- |
| `~/.nexus/` (`$NEXUS_HOME`) | machine and user state root |
| `~/.nexus/nexus.db` | shared SQLite state: every project's sessions ([sessions.md](sessions.md)); file `0600`, dir `0700` |
| `~/.nexus/credentials.json` | provider secrets and OAuth refresh records; plaintext, owner-only `0600`, parent `0700` |
| `~/.nexus/config.toml` | user config; Settings → Providers/Setup write `[providers.*]` and `[models].default` here |
| `~/.nexus/{agents,skills,tools,providers}/`, `mcp.json`, `hooks.toml`, `SOUL.md` | user-scope extensions |
| `~/.nexus/daemon/<hash>.sock` (+ pid, lock, log, http discovery) | one daemon per workspace ([host.md](host.md)); falls back to a private short dir when the path is too long |
| `~/.nexus/locks/sessions/<project-hash>/` | per-session `flock` files |
| `~/.nexus/projects/<project-hash>/` | per-project machine state: cache, logs (incl. `logs/mcp/`), `stage/`, extension trash |
| `~/.nexus/cache/` | shared models.dev cache, `update-check.json` |
| `~/.nexus/models/voice/` | downloaded dictation model ([voice.md](voice.md)) |
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

`NEXUS_COMPLETION_SOUNDS=off` (also `0` or `false`) disables the native client's
generated completion audio cue independently of `NEXUS_VOICE_SOUNDS` recording
cues. Audio playback requires `sounddevice` and a working output device.

`NEXUS_HOME` (state root), `NEXUS_DEV` (dev mode), `NEXUS_HTTP`, `NEXUS_HTTP_HOST`,
`NEXUS_HTTP_PORT`, `NEXUS_HTTP_TOKEN`, `NEXUS_HTTP_ORIGINS` (opt-in HTTP/SSE
surface, [host.md](host.md)), `NEXUS_NO_UPDATE_CHECK`, `NEXUS_VOICE=off`.
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
