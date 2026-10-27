# Plan 05: Add an MCP server from Settings (project or global)

Status: implemented and verified (2026-10-20). Host protocol `SettingsMcpAdd` and Ratatui Settings MCP editor verified by tests (`.venv/bin/python -m pytest` feature suite: 781 passed; `cargo test --locked rust/tui`: 590 passed 2 ignored plus 58 dashboard passed). Fixture editor, preview, and result visually inspected in native captures (`artifacts/native-final`). Real isolated install verified separately for MCP `filesystem@2.0.0`. Source: root `TODO.md` item 2
("new MCP add ( project level or global level )").

Implementation deltas from this plan (see `docs/extensions.md`, "Adding an MCP server from
Settings"): the form is a one-server JSON editor plus a masked confirmation (no field-by-field
form, no CLI, no web/desktop); the host command is `SettingsMcpAdd(scope, name, entry,
expected_sha256=None)` and does **not** reuse `write(...)` (redaction restoration could copy
other servers' secrets and whole-file validation would block on invalid siblings): it shares
only the extracted `_atomic_write`; names are `^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$`; result
statuses `written | exists | conflict | legacy` with a `message`; a project add is refused
while only the legacy `.nexus/mcp.json` exists; a global add is refused when `NEXUS_HOME`
makes the host read a different folder than Settings writes (pre-existing split, documented).

## 1. Intent

Settings → MCP servers (Ratatui) gets an **Add server…** action per scope
(global `~/.nexus/mcp.json`, project `<workspace>/.agents/mcp.json`). The user
fills a short form (or pastes one JSON entry) and Nexus inserts exactly one new
server into that file without disturbing anything else, then hot-applies it.

### Non-goals

- Editing or removing existing servers beyond today's Enabled/Tool loading
  controls (the raw `Edit…` file editor stays the route for that).
- OAuth flows (not implemented, `docs/extensions.md:160`), a server catalog,
  or connecting/testing the server during add.
- Desktop (GPUI) work (paused, `AGENTS.md:8`); web parity is a later plan.
- Writing to the legacy `.nexus/mcp.json` (read-only fallback).

## 2. Current evidence

- Page: `nexus/ui/ratatui/settings_pages/mcp.py:89-114` lists both scopes, each
  with `New file`/`Edit…`/`Reset` from `files.file_blocks`; only
  `enabled`/`loading` operations exist (`mcp.py:117-133`).
- Host edits: `set_mcp_enabled`/`set_mcp_loading` read the file, check
  `expected_sha256`, patch one value with a byte-preserving JSONC token scan, and
  write (`nexus/host_support/settings_inventory.py:237-335`). Strict parsing
  refuses duplicate keys/ambiguous aliases (`_patch_mcp_value`, :263-280).
- Protocol: `SettingsMcpLoadingSet`, `SettingsMcpEnabledSet`
  (`nexus/host/protocol.py:142-155`); client wrappers at
  `nexus/client/protocol.py:151-155`.
- Accepted entry dialects and interpolation: `docs/extensions.md:145-168`
  (`client.normalize_server_entry`). Config merge: project names win over
  global (`docs/extensions.md:127-131`).

## 3. Design

1. **Patch helper** `insert_mcp_server(body, name, entry) -> str` beside
   `_patch_mcp_value`:
   - Empty or missing file → `{"mcpServers": {name: entry}}` formatted with
     two-space indent.
   - Otherwise strict-parse; the server map is the single existing map key
     (`_server_maps`); none → add `"mcpServers"`; several → use the first in
     `_MCP_SERVER_MAPS` order and say so in the result.
   - Name already present in **any** map of that file → `conflict` error.
   - Insert as the last property of the map using the same token-span approach,
     preserving every other byte and comment.
2. **Validation before write**: run the entry through `normalize_server_entry`
   so the same rules that load the file accept it; reject unknown keys, `ws`,
   `${input:…}`. Server names: `[A-Za-z0-9_.-]{1,64}`.
3. **Host command** `SettingsMcpAdd(scope, name, entry: dict, expected_sha256)`
   → `SettingsWriteResult`; `expected_sha256` of the empty string means "file
   must not exist". Reuses `write(...)`, so path policy, bounds, redaction and
   hot reload are unchanged.
4. **Ratatui form** (`Add server…` button under each scope heading): fields
   name, transport (stdio / HTTP / SSE), command + args *or* URL, optional env
   and headers (shown masked after save), Enabled, Tool loading
   (Search default). A "Paste JSON" mode accepts one `{name: entry}` object in any
   accepted dialect. Footer flash: "<name> added · applies to new sessions"
   (same wording as `mcp.py:133`). A project-scope add whose name exists in
   global shows a note that the project entry wins.
5. **CLI** (optional): `nexus mcp add <name> [--global] -- <command> [args…]`
   or `--url`, using the same host command.

## 4. Tests and docs

- `tests/test_settings_mcp_add.py` (new): insert into missing/empty/JSONC-with-
  comments files (byte-preserved elsewhere); each server-map dialect; duplicate
  name conflict; stale hash conflict; invalid entry rejected with no write;
  secret values redacted in results; project and global targets.
- `tests/test_ratatui_settings_files.py` or a new `tests/test_ratatui_settings_mcp.py`:
  button per scope, form → operation shape, conflict flash.
- Docs: `docs/extensions.md` (Settings files / MCP), `docs/surfaces.md`
  (Settings), `docs/cli.md` if the CLI lands, `docs/module-map.md` if a module is added.

## 5. Acceptance criteria

- [x] Adding a stdio server in Global writes one new entry to `~/.nexus/mcp.json`;
      every other byte of the file is unchanged.
- [x] Adding in Project creates `.agents/mcp.json` when absent (never writes
      `.nexus/mcp.json`).
- [x] The new server appears on the page and in `/mcp` without a restart;
      existing sessions are unaffected until they pick it up per current rules
      (`docs/extensions.md:243-247`).
- [x] Duplicate names, invalid entries and stale hashes write nothing and say why.
- [x] Offline tests pass (part of feature suite: 781 passed; `tests/test_settings_mcp_add.py`).

## 6. Risks and open decisions

- Secrets typed into env/header fields are stored in plain JSON like hand-edited
  files today; the form should suggest `${env:VAR}` interpolation. Open: offer
  an "env reference" helper. Default: hint text only.
- Multi-map files are rare; choosing the first map is deterministic but may
  surprise a user who hand-maintains `servers`. Reported in the result.

## 7. Verification ledger

- Tests independently verified by parent: `.venv/bin/python -m pytest` (781 passed, including `tests/test_settings_mcp_add.py`, `tests/test_ratatui_settings_simple_pages.py`) and `cargo test --manifest-path rust/tui/Cargo.toml --locked` (590 passed, 2 ignored + 58 dashboard passed).
- Visual inspection: parent inspected native fixture captures in `artifacts/native-final` (`fixture-mcp-editor.png`, `fixture-mcp-preview.png`, `fixture-mcp-result.png`).
- Real isolated install: verified separately for MCP `filesystem@2.0.0`.
- Limitations: Ratatui single-server JSON editor with masked confirmation only (no field-by-field form, no CLI subcommand, desktop/web deferred); project add is refused when only legacy `.nexus/mcp.json` exists; global add is refused when `NEXUS_HOME` diverts host settings.
