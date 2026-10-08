# Extensions: hot reload, skills, hooks, MCP, Settings files

Two tiers (how to write each is in [extending.md](extending.md)):

- **Data** (skills, agents, hooks, MCP servers, `nexus.toml`, `SOUL.md`,
  `MEMORY.md`) is parsed, not imported, and reloads freely.
- **Code** (`.py` tools, in-process hooks, file providers) is loaded through
  quarantine under a generation-stamped module name.

Extension files are **trusted code**. Quarantine validates; it does not sandbox
([security.md](security.md)).

## Hot reload (`ext/`)

| File | Owns |
| --- | --- |
| `manifest.py` | immutable `Manifest` (config, tools, skills, agents, hooks, MCP state, system files, module handles), `ManifestRef` (atomic reference, monotonic generation, `ManifestLease` pins), `ManifestDiff`, `ReloadReport` |
| `manager.py` | `ExtensionManager`: the only writer of the manifest ref; serialized, coalescing rebuilds; trash |
| `quarantine.py` | validate a candidate file before import |
| `template.py` | the seeded `.agents/tools/_template.py` (leading `_` = support file, never loaded) |
| `nexus/tools/loader.py` | (outside `ext/`) imports the frozen bytes under `nexus_ext.<label>_<hash>__g<generation>`; never `importlib.reload` |

Every trigger (directory watcher at `ext.watch_interval_ms`, the
`ReloadExtensions` tool, `ExtensionsReload`, `nexus ext reload`) runs one rebuild:

1. load effective config (a config that fails to load keeps the old manifest);
2. discover tools, skills, agents, hooks, MCP definitions and system files;
3. reuse unchanged extensions by content hash; re-quarantine changed ones;
4. **all-or-nothing:** any failure releases everything this attempt loaded and
   keeps the previous manifest;
5. one compare-and-swap of the immutable manifest (a no-op does not churn the
   generation);
6. retire a superseded generation only after its last lease is released, so an
   in-flight call keeps running its own generation.

The loop pins one generation per iteration ([loop.md](loop.md)). Events:
`ext.loaded`, `ext.unloaded`, `ext.failed`, `ext.tool_shadowed`,
`ext.manifest_changed`. Reports and events are JSON-safe, credential-free and
never carry source bodies.

**Quarantine stages:** bounded single read (regular file, no symlink, ≤
`ext.max_file_bytes` 262,144, UTF-8, no NUL) → static inspection (`ast.parse`,
dangerous import-time scan, `SPEC`/`register()` shape) → import in an isolated
minimal-environment subprocess with a hard timeout (10s) → stage a
content-addressed copy under `~/.nexus/projects/<hash>/stage/` and require its
hash to match before the in-process import. Refusal codes are in
`QuarantineCode` (e.g. `syntax_error`, `no_contract`, `bad_schema`,
`builtin_collision`, `import_timeout`, `hash_mismatch`).

**Deliberately not hot:** core Nexus modules, new pip installs, and the manifest
shape. `nexus doctor --explain-reload` states the boundary.

**Trash:** `ExtensionManager.trash(target)` is the only operation that removes a
trusted extension file: scoped to managed roots, refuses symlinks, traversal and
non-candidates, atomically moves to a retention-recorded trash entry and
rebuilds; a failed rebuild rolls the move back unless forced. Exposed as
`nexus ext trash` / `ExtensionsTrash`. There is no CLI restore.

Roots: tools under `<workspace>/.agents/tools/` (and legacy `.nexus/tools/`) then
`~/.nexus/tools/`, workspace first. File providers
(`model/providers/discovery.py`) use the same quarantine seam.

## Skills (`skills/`)

A skill is a directory with `SKILL.md`. Discovery roots (precedence workspace >
user > builtin): `<workspace>/.agents/skills/` (and legacy `.nexus/skills/`),
`~/.nexus/skills/`.

- Frontmatter (restricted, six keys): `name`, `description`, `allowed-tools`,
  `bundles`, `model`, `version`; `name` and `description` required.
- **Progressive disclosure:** the context gets only sanitised `name: description`
  lines (`SkillManager.render_index`); the body loads through the `skill` tool
  (`SkillInvocation`, delimited and bounded, with provenance and hashes).
- Bundled `scripts/` and `references/` resolve through a fail-closed resolver
  (`resources.py`); bundled `tools/*.py` register only while the skill is active.
- `SkillActivation` is an immutable overlay that **narrows** the turn's tools:
  `active = (available ∩ profile) ∩ declared`. A declaration never grants; unknown
  bundles fail closed to the empty set; declaring nothing does not narrow.
- Per session, an individual skill (or tool, `category="tools"`) can be switched off (`ContextExtensionSelect`);
  disabled skills leave the index, invocation and bundled tools. Locked after the
  first turn.
- Events: `skill.invoked`, `skill.completed`.
- Host `SkillInspect` is independent of invocation and is permitted for disabled
  skills. It leases the manifest and displays refresh-time declaration bytes
  (`parsed.raw`) and body, without reading the live file, activating bundled
  tools, or changing session events. `frontmatter_text` is explicitly the
  declaration payload, not a reconstructed `SKILL.md`. Host text is redacted;
  body output is byte-bounded with explicit truncation and missing-snapshot
  errors. Refresh is required to observe later file changes.

## Hooks (`hooks/`)

Deterministic behavior the model cannot skip. A hook attaches to an event and
returns a `HookDecision`.

| Events | `SessionStart` `UserPromptSubmit` `ContextAssembled` `PreToolUse` `PostToolUse` `PreCompact` `TurnEnd` `SessionEnd` `ExtensionLoaded` |
| --- | --- |
| Decisions | `allow`, `warn`, `block` (a tool block becomes an error result the model sees), `modify` (replaces the input; the caller **must revalidate and re-gate**: a hook is policy, not a permission) |

- **Command hooks:** `[[hooks.<Event>]]` tables in `.agents/hooks.toml` (falling
  back to legacy `.nexus/hooks.toml`, then `~/.nexus/hooks.toml`); `matcher` uses
  the permission-rule grammar; `command` is argv with no shell unless
  `shell = true`; `on_nonzero` = `block|warn|ignore`; timeout default 10s (max 600s); stdin
  JSON (≤ 256 KiB), bounded `NEXUS_*` env ([config.md](config.md#other-environment-variables)),
  fixed safe OS env, stdout ≤ 64 KiB.
- **Python hooks:** `.agents/hooks/*.py` (`HOOKS` or `register()`), loaded through
  quarantine; loading emits `TRUSTED_CODE_WARNING`. Project shadows legacy
  shadows user by file stem.
- Hooks run in `Runtime._HookService`, not in `core`. `SessionStart` and
  `UserPromptSubmit` run in the session layer before the prompt is persisted, so a
  block leaves no orphan message. All failures are isolated and sanitised.
- Events: `hook.fired`, `hook.blocked`. `[hooks] enabled = false` disables all.

## MCP (`mcp/`)

Transport, lifecycle and translation are three files; nothing else imports the
upstream `mcp` package.

| File | Role |
| --- | --- |
| `client.py` | `MCPClient`: stdio (argv, never a shell; process-group termination), Streamable HTTP and legacy SSE behind normalized `MCP*` types; deadlines on connect/initialize/list/call; bounded, redacted, never-surfaced stderr |
| `manager.py` | `MCPManager`: lazy single-flight connect, per-server health, exponential backoff, circuit breaker (half-open after cooldown), list cache (keyed by server version + config fingerprint), hot `apply`, one immutable `MCPSnapshot` |
| `bridge.py` | tools → `mcp__<server>__<tool>` in bundle `mcp`; `mutates = True` unless `readOnlyHint` is explicitly true; resources via `ReadMcpResource`; prompts as slash data |
| `errors.py` | normalized taxonomy (`MCPTimeout`, …) |

Health: `disabled`, `unknown`, `connecting`, `ready`, `degraded`, `backoff`,
`failed`. A dead server never fails a turn: its tools vanish, `mcp.failed` is
emitted, the rest are untouched. Config: `.agents/mcp.json` (JSONC), merged over
`~/.nexus/mcp.json` (project names win; the legacy `.nexus/mcp.json` is read only
when `.agents/mcp.json` is absent; both project candidates are watched). See
[MCP config dialects](#mcp-config-dialects) for the accepted keys. `[mcp]`:
`connect_timeout_s` 20, `restart_max` 5. Each file is isolated: a corrupt file
keeps its own previous servers and is reported, never hiding the other file's.
Stderr goes to `~/.nexus/projects/<hash>/logs/mcp/`, never into context.
Stdio servers default to the selected workspace as their working directory;
an explicit relative `cwd` is resolved against that workspace, and an absolute
`cwd` is preserved. Relative script arguments therefore work regardless of the
daemon's launch directory.
**Everything an MCP server says is untrusted data:** sanitised of control/bidi
characters, bounded, wrapped in `<untrusted-mcp-data>` with a no-authority
notice; the permission engine remains the boundary. Events: `mcp.connected`,
`mcp.disconnected`, `mcp.failed`, `mcp.tools_changed`. Per session a server can be
switched off (`ContextExtensionSelect`).

## MCP config dialects

`mcp.json` accepts what other clients write, so a file copied from VS Code,
Claude Code/Desktop, Cursor, Cline, Roo, Windsurf, Gemini CLI, OpenCode or Zed
loads unchanged (`client.normalize_server_entry`; tests in `test_mcp_dialects.py`).

| Where | Accepted |
| --- | --- |
| Top level | server maps `servers` (VS Code), `mcpServers` (most), `mcp` (OpenCode), `context_servers` (Zed); several may coexist, the first definition of a name wins and the duplicate is reported. `$schema`, `inputs`, `sandbox` are ignored; other keys are a warning, not a failure |
| Transport | `transport`, `type` or `transportType`: `stdio`/`local`, `http`/`streamable-http`/`streamableHttp`/`remote`, `sse`. `ws` is refused. A `url` with no transport is HTTP, or SSE when the path ends in `/sse` |
| Process | `command` as a string, an argv list (OpenCode) or `{path, args, env}` (Zed); `args`; `env` or `environment`; `envFile` (dotenv, bounded 64 KiB, explicit `env` wins); `cwd` |
| Remote | `url`, `serverUrl` (Windsurf), `httpUrl` (Gemini, HTTP); `headers` |
| Switch | `enabled` or `disabled` (Settings flips whichever the entry uses) |
| Limits | `*_timeout_s`; `timeout` → call timeout, milliseconds when ≥ 1000 (Claude Code, OpenCode, Gemini) otherwise seconds (Roo) |
| Tools | `tool_loading`; `alwaysLoad: true` (Claude Code) = `"all"`; `includeTools`, `excludeTools`/`disabledTools` filter the listed tools |
| Shown, not acted on | `autoApprove`, `alwaysAllow`, `trust`, `oauth`, `auth`, `headersHelper`, `description`, `gallery`, `version`, `dev`, `icon`, `source`, `networkTimeout`, `settings`, `sandboxEnabled`, `watchPaths` (approval stays with the permission engine; OAuth is not implemented) |

Any other key is an error for that server only. Interpolation in `command`,
`args`, `env`, `envFile`, `cwd`, `url`, `headers`: `${env:VAR}`, `${VAR}`,
`${VAR:-default}`, `{env:VAR}`, `${workspaceFolder}`, `${workspaceFolderBasename}`,
`${userHome}`, `${pathSeparator}`, `${/}`. A missing variable without a default is
an error naming it; `${input:id}` is refused (Nexus never prompts); bare `$VAR` is
literal. Resolved values are secrets (redacted everywhere). Not verified against
live GitHub Copilot MCP or OAuth-only servers.

`InspectContext.mcp_servers` carries, per server, `scope`, `source_path`,
`url`, `ignored_keys`, the tool filters and a `status` of `connected`,
`disabled`, `failed`, `backoff`, `connecting` or `not connected` (lazy, never
tried). Entries that failed to parse are rows with `invalid: true`; unusable
files and file warnings are rows with `file_error: true` (name = the file).

## Settings files (host)

The Settings console edits these through `Settings*` commands
(`host_support/settings_inventory.py`, `settings_scope.py`), in global
(`~/.nexus/`) or project (`<workspace>/.agents/`) scope:

| Category | File(s) |
| --- | --- |
| `agents` | `agents/<name>.md` (built-ins shown read-only; edit = override; delete = restore) |
| `skills` | `skills/<dir>/SKILL.md` |
| `tools` | `tools/<name>.py` (not `_*`) |
| `hooks` | `hooks.toml` |
| `mcp` | `mcp.json` |
| `config` | `nexus.toml` (project) / `config.toml` (global) |
| `soul` | `SOUL.md` |

Writes are path-policed (one policy in `settings_scope.py`), bounded (512 items,
256 KiB bodies), hash-checked (`expected_sha256`) and redacting: secret-looking
fields display masked and unchanged masked values are restored on save. Reset and
delete move previous content to settings trash. UI behavior:
[surfaces.md](surfaces.md#settings).

Settings → Agents uses one shared user configuration in `~/.nexus/agents/`,
with no global/project selector. Other file categories retain scoped editing.

## Testing

`tests/test_ext_*.py`, `test_hot_*.py` (including the 200-reload leak test and
the write-a-tool-and-call-it walkthrough), `test_skills_*.py`, `test_hooks_manager.py`,
`test_mcp_*.py`, fixtures under `tests/fixtures/{extensions,hooks,mcp}` and
`tests/fixtures/mcp_server.py`.

## MCP search loading

### Read-only server detail

`McpServerShow(session, name)` reads the MCP manager's already-retained state;
it never initializes or connects a server, refreshes catalogs, or invokes tools.
Detail includes connection health, a stdio command **basename and argument count**
(never argument values, env, headers, working directory, or endpoint URL), retained
initialize server info/instructions, full tool input schemas, resources/templates,
and prompts. Unconnected catalogs and metadata not retained by the bridge (raw
annotations and per-tool sent state) are explicitly marked unavailable. Connected
empty catalogs remain empty rather than being fetched again.

Display data is redacted at the host boundary, including configured MCP secrets.
The UTF-8 snapshot limit defaults to 256 KiB (maximum 1 MiB); oversized snapshots
return a labeled, possibly incomplete JSON preview with `clipped=true`, never a
silently shortened schema. Each resource/template/prompt catalog is limited to
256 retained entries with its own explicit clipping flag. Inspection does not
alter loading or selection state.

Servers default to `tool_loading: "search"`; their full schemas stay out of
`ModelRequest.tools`. `McpSearch` discovers tools with local deterministic
keyword ranking or exact `select:` queries; `McpCall` validates and invokes a
current target. Search never invokes a target and cannot connect disabled
servers. Target removal returns “not found; search again”. Per-query server
failures do not discard other queries. Search results carry names-only notes
for compaction, and retain the existing untrusted-data fencing.

`nexus/mcp/search.py` indexes at most 2,000 tools per server and 64 servers.
Names outrank parameter names, which outrank description matches; IDF weights
reduce common-word bias. Ties sort by qualified name. Bounds are announced.
Search caches the immutable catalogue identity; no embeddings or model requests
are involved. Individual schemas cap at 8,000 characters, or 24,000 for a
single-tool exact selection; the total obeys `tools.max_result_tokens`.

A session can choose Search, Load all, or Follow configuration per server.
Choices are durable events and lock after the first turn. Effective modes freeze
in `context.mcp_loading_frozen`; subsequent config edits affect new sessions.
Servers added to an existing session use Search. All loading retains the direct
`mcp__server__tool` interface.
