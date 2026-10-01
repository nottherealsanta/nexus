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
- Per session, an individual skill can be switched off (`ContextExtensionSelect`);
  disabled skills leave the index, invocation and bundled tools. Locked after the
  first turn.
- Events: `skill.invoked`, `skill.completed`.

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
emitted, the rest are untouched. Config: `.agents/mcp.json` (JSONC) `servers` (or
`mcpServers`) map, merged over `~/.nexus/mcp.json` (project names win; the legacy
`.nexus/mcp.json` is read only when `.agents/mcp.json` is absent); per server
`transport`, `command`, `args`, `env`, `cwd`, `url`, `headers`, `*_timeout_s`;
unknown keys are errors; only `${env:VAR}` interpolates. `[mcp]`:
`connect_timeout_s` 20, `restart_max` 5. A corrupt file keeps the previous set.
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
