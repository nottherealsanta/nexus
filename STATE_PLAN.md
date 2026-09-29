# STATE_PLAN — sessions in one SQLite DB under `~/.nexus`, project files under `.agents/`

> **Superseding development-phase note (2026-09-29):** This is a historical
> implementation plan; its migration and downgrade instructions below do not
> describe current behavior. Current session storage is SQLite-only, with JSONL
> available for export. Old session and trash files are not imported. Export
> sessions before switching to this storage if they need to be retained. The
> `.nexus/` read fallback applies to project extensions and settings only.

Status: implemented 2026-09-29. Shared SQLite sessions and `.agents/` project
extensions are the current convention; `<workspace>/.nexus/` remains a legacy
read fallback for project extensions and settings only. The checks in §6 and
historical phases in §8 document the rollout rather than pending work.

## 1. Goal

Today every workspace grows a `<project>/.nexus/` directory holding session logs,
locks, trash, archive sidecars, caches, logs, staging, and the project's
extensions (skills, agents, tools, hooks, providers, `mcp.json`, `nexus.toml`).

Target:

| What | Where today | Where after |
| --- | --- | --- |
| Session logs (`<id>.jsonl`), snapshots (`.snap.json`), archive index/cursor, trash + `meta.json`, child-agent logs (`sessions/agents/`) | `<project>/.nexus/sessions`, `<project>/.nexus/trash` | **one SQLite DB for all projects: `~/.nexus/nexus.db`** |
| Session locks (`<id>.lock`, `.archive.lock`, `trash.lock`) | `<project>/.nexus/sessions/` | `~/.nexus/locks/sessions/<project-hash>/` (flock files; OS releases on death) |
| Caches (`models.dev.json`, `tokens/`, `mcp/`), logs (`logs/`, `logs/mcp/`), `stage/`, extension trash | `<project>/.nexus/…` | `~/.nexus/projects/<project-hash>/{cache,logs,stage,trash}` |
| Skills, agents, tools, hooks (`hooks/`, `hooks.toml`), providers, `mcp.json`, project settings `nexus.toml` | `<project>/.nexus/…` | **`<project>/.agents/…`** |
| User-scope config/extensions | `~/.nexus/…` | unchanged (`~/.nexus/config.toml`, `~/.nexus/skills`, …) |
| Daemon socket/pid/log | `~/.nexus/daemon/` | unchanged |

After the change a normal `nexus chat` / `nexus run` / `nexus web` in a fresh
project writes **nothing** into `<project>/.nexus/`. The only project-local
directory Nexus creates is `.agents/`, and only when the user (or the model via
`WriteTool`, Settings, or agent seeding) actually writes an extension there.

`<project>/.nexus/` becomes a read-only **legacy** location: its sessions are
imported once into the DB, and its extensions are still *read* (lower precedence
than `.agents/`) with a `nexus doctor` warning, so nobody loses anything.

Non-goals: moving `nexus.toml` at the project root, changing the host protocol
wire shapes, changing the reducer, moving `.nexus-worktrees-*` sibling dirs
(tracked as follow-up §10).

## 2. Invariants to keep

These are the contracts the current file store guarantees; the SQLite store must
keep every one of them.

1. **Durable log first / append-only.** Records are only ever inserted. `seq`
   is monotonic per session. Replay of the stored records through
   `nexus/view/reduce.py` reproduces live state (AGENTS.md rule 5).
2. **Identical record encoding.** A record row stores exactly
   `msgspec.json.encode(EventRecord | MessageRecord | SummaryRecord)` — the
   same bytes a JSONL line holds today. Export (`json`/`md`/`jsonl`), snapshot
   validation, replay, fork and the reducer do not change.
3. **Durability per append.** Each append is one committed transaction with
   `PRAGMA synchronous=FULL` (WAL). This replaces "write + fsync per line".
   Crash-tail repair disappears (a transaction is atomic), so `ReadResult`
   always has `truncated_tail=False`, `ends_with_newline=True`.
4. **Concurrency.** One exclusive writer per session (turn execution) enforced
   by the existing `SessionLock` flock — unchanged semantics, relocated lock
   files. Readers (`fork`/`replay`/`list`/`export`) never block on a writer:
   WAL readers see a consistent committed prefix, so the "shared-lock-or-lock-free
   read" dance becomes simply "read in a transaction".
   Several daemons (one per workspace) share the DB: WAL mode,
   `busy_timeout=5000`, `BEGIN IMMEDIATE` for writes, short transactions only.
5. **Fork never overwrites.** Uniqueness is the `PRIMARY KEY (project_id,
   namespace, id)` on `sessions`; fork is `INSERT session` + `INSERT … SELECT`
   of the prefix in one transaction. A duplicate id raises `SessionError`
   ("Session log already exists"), same message as today.
6. **Delete never cancels a turn; delete/restore fail fast** with `SessionBusy`
   when the exclusive lock is held. Trash retention (7 days default) and
   restore semantics unchanged.
7. **Secrecy boundary.** Credentials never enter the DB. DB file mode `0600`,
   `~/.nexus` mode `0700`. Errors crossing the host boundary stay redacted.
8. **Bounded everything.** Search/preview keep their byte/row caps
   (`MAX_SEARCH_TAIL_BYTES`, `MAX_SEARCH_SESSIONS`, `MAX_ARCHIVED_PAGE`);
   queries use `LIMIT`.
9. **Layering** (`tests/test_layering.py`): the DB lives in `nexus/session/`
   and imports only `config/errors/events/util/model` like `store.py` does.
   UI never touches it (rule 4).

## 3. Paths module

Add to `nexus/config/paths.py` (lowest layer, so every manager can use it):

```python
def nexus_home(home: Path | None = None) -> Path:       # ~/.nexus  ($NEXUS_HOME overrides)
def state_db_path(home=None) -> Path:                   # ~/.nexus/nexus.db
def project_key(workspace: Path) -> str:                # sha256(realpath)[:16] – same as daemon.workspace_hash
def project_state_dir(workspace, home=None) -> Path:    # ~/.nexus/projects/<key>
def project_agents_dir(workspace) -> Path:              # <workspace>/.agents
def legacy_project_dir(workspace) -> Path:              # <workspace>/.nexus   (read-only fallback)
def workspace_settings_config_path(workspace)           # now <workspace>/.agents/nexus.toml
```

`nexus/host/daemon.py:workspace_hash` delegates to `project_key` so the socket
name and the project row agree. `NEXUS_HOME` lets tests and power users relocate
all global state; every constructor that already takes `home=` keeps doing so.

## 4. Database

### 4.1 File & connection

`nexus/session/db.py` — `StateDatabase(path)`:

* `sqlite3` (stdlib), one connection per thread (`threading.local`),
  `check_same_thread=False` not needed.
* On open: `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`,
  `busy_timeout=5000`, `temp_store=MEMORY`. Create parent dir `0700`, file
  `0600` (`os.open(..., 0o600)` before first connect).
* `user_version` pragma holds the schema version; `migrate_schema()` runs
  forward-only DDL steps inside `BEGIN IMMEDIATE`. A DB newer than the code
  raises `SessionError("state database schema N is newer than this Nexus")`.
* `transaction()` context manager (`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK`),
  `read()` context manager (`BEGIN` deferred) for consistent multi-query reads.
* Integrity: `nexus doctor` runs `PRAGMA quick_check` and reports size/WAL.

### 4.2 Schema v1

```sql
CREATE TABLE projects (
  id          TEXT PRIMARY KEY,          -- project_key(realpath)
  root        TEXT NOT NULL,             -- last known realpath (display, doctor)
  created_at  REAL NOT NULL,
  last_opened REAL NOT NULL,
  legacy_imported_at REAL                -- set once .nexus/sessions was imported
);

CREATE TABLE sessions (
  project_id  TEXT NOT NULL REFERENCES projects(id),
  namespace   TEXT NOT NULL DEFAULT 'main',   -- 'main' | 'agents' (child sessions)
  id          TEXT NOT NULL,                  -- validate_session_id()
  created_at  REAL NOT NULL,
  last_seq    INTEGER NOT NULL DEFAULT 0,     -- denormalized, updated in the append txn
  last_activity REAL NOT NULL DEFAULT 0,
  message_count INTEGER NOT NULL DEFAULT 0,
  title       TEXT NOT NULL DEFAULT '',
  parent_id   TEXT NOT NULL DEFAULT '',
  fork_seq    INTEGER NOT NULL DEFAULT 0,
  archived_at REAL, archive_reason TEXT,      -- replaces archive.json
  trash_id    TEXT UNIQUE, trashed_at REAL,   -- replaces trash/<id>/ + meta.json
  trash_expires_at REAL, trash_reason TEXT,
  PRIMARY KEY (project_id, namespace, id)
);

CREATE TABLE records (
  project_id TEXT NOT NULL, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
  seq   INTEGER NOT NULL,
  kind  TEXT NOT NULL CHECK (kind IN ('event','message','summary')),
  ts    REAL NOT NULL,
  body  BLOB NOT NULL,                        -- exact msgspec-encoded record
  PRIMARY KEY (project_id, namespace, session_id, seq),
  FOREIGN KEY (project_id, namespace, session_id)
     REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE snapshots (                      -- derived cache, replaces <id>.snap.json
  project_id TEXT, namespace TEXT, session_id TEXT,
  seq INTEGER NOT NULL, body BLOB NOT NULL,
  PRIMARY KEY (project_id, namespace, session_id),
  FOREIGN KEY (project_id, namespace, session_id)
     REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
);

CREATE TABLE kv (project_id TEXT, key TEXT, value TEXT,
                 PRIMARY KEY (project_id, key));   -- archive.cursor etc.

CREATE INDEX sessions_by_activity ON sessions(project_id, namespace, last_activity DESC);
```

Notes:
* Summary fields (`title`, `message_count`, `last_activity`, `created_at`,
  `parent_id`, `fork_seq`) are maintained in the same transaction as the append
  so `SessionManager.list()` becomes one indexed query instead of reading every
  log. They are *derived*; `nexus doctor --repair` (or a schema step) can
  recompute them from `records`.
* Title derivation must match whatever `_summary_for` does today — reuse that
  function on the record being appended, don't reimplement it.
* Trash keeps rows in place with `trash_id` set; list/open/exists filter
  `trash_id IS NULL`. `purge_expired` = `DELETE FROM sessions WHERE
  trash_expires_at < ?` (cascade). Restore clears the columns (and refuses if a
  live session with that id exists, as today).
* `SESSION_LOG_VERSION` stays in each record body; unknown `v` still fails
  closed on read.

## 5. Code changes by module

### 5.1 `nexus/session/`

* **`db.py` (new)** — `StateDatabase` (§4.1) + `SqliteSessionStore(db,
  project_id, namespace="main")` implementing the exact `SessionStore` public
  surface used by `session.py`, `manager.py`, `snapshot.py`, `export.py`,
  `observability/session.py`, runtime and facade:
  `exists, create, create_from_records, read, records, next_seq,
  append_event, append_message, append_summary, fsync` (no-op callable kept for
  injected-durability tests), `lock_path` (→ lock dir, §1). `log_path` is
  removed from production callers; keep it only on the legacy store.
  Seq assignment: `SELECT last_seq FROM sessions` inside the `BEGIN IMMEDIATE`
  txn, so two processes can never assign the same seq; an explicit requested
  seq that collides raises `SessionError` (PK violation).
* **`store.py`** — rename class to `JsonlSessionStore`, keep it (and its
  crash-tail logic + tests) as the **legacy reader** used only by the importer.
  Keep `SessionStore = JsonlSessionStore` alias only if tests need it during the
  transition; production must not construct it.
* **`import_legacy.py` (new)** — `import_workspace_sessions(db, project_id,
  legacy_dir)`: for `<project>/.nexus/sessions/*.jsonl` (+ `agents/*.jsonl`,
  v1 `*.json` via existing `migrate.py`, `archive.json`, `archive.cursor`,
  `.snap.json`, `<project>/.nexus/trash/<entry>/meta.json` sessions): read with
  `JsonlSessionStore.read` (repairs nothing on disk; a crash tail is simply
  dropped), insert in one transaction per session, skip ids already in the DB
  (idempotent), then set `projects.legacy_imported_at`. Afterwards **rename**
  `<project>/.nexus/sessions` → `<project>/.nexus/sessions.imported-<ts>` (and
  `trash` likewise). Never delete user data. Hold a flock on
  `~/.nexus/locks/import-<key>.lock` so two daemons can't double-import. A
  corrupt legacy log is left in place, logged, and reported by `doctor`; it
  doesn't block the rest.
* **`manager.py`** — constructor becomes
  `SessionManager(directory=None, *, db=None, project=None, namespace="main",
  store=None, lock_dir=None, …)`.
  - Production: `SessionManager(db=StateDatabase(state_db_path(home)),
    project=workspace, lock_dir=…)`.
  - **Test compatibility:** `SessionManager(tmp_dir)` keeps working by opening a
    private DB at `tmp_dir/nexus.db` with `project=tmp_dir`, so the ~125
    existing call sites stay hermetic with no edits.
  - Replace file-based archive index / cursor / `.archive.lock`, trash
    directories / staging / `meta.json` / `_recover_trash*`, `_session_ids`
    directory scan, `_artifact_paths`, `_looks_like_legacy_session` with SQL.
    `ArchiveRecord`/`TrashRecord`/`SessionSummary` structs and every public
    method signature stay identical (host facade and `session_archive.py`
    depend on them).
  - `path(session_id)` → returns a descriptive pseudo-path is **not** OK;
    audit callers and replace with store calls (see §5.3).
  - `_consistent_read` → single read transaction.
  - `migrate()` (v1 `.json`) moves into the importer; keep the method as a
    thin wrapper for compatibility.
* **`snapshot.py`** — read/write the `snapshots` table (upsert in one txn);
  validation logic unchanged.
* **`lock.py`** — unchanged mechanics; callers pass the relocated path.
* **`session.py`** — should only need changes where it touches paths
  (snapshot path, lock path). Audit `grep -n "Path\|path" session.py`.

### 5.2 Runtime wiring (`nexus/runtime.py`)

* L≈2205: build the manager from `StateDatabase(state_db_path(self._home))`
  + `project=self.workspace`; keep the `session_dir=` override (tests) mapping
  to the private-DB constructor.
* L≈4100 `_ensure_child_sessions`: `_ChildSessionFacade` gets the same DB with
  `namespace="agents"` instead of `directory/"agents"`.
* Run the legacy import once at runtime start (off the event loop via
  `asyncio.to_thread`, bounded, never failing startup).
* Caches: `…/.nexus/cache/tokens` (L≈2101), `cache/models.dev.json` (L≈3572 and
  `host_support/setup.py:86`), `cache/mcp` (L≈4979) → `project_state_dir()/cache/…`.
  `models.dev.json` is not project specific: put it at `~/.nexus/cache/`.
* `_settings_agent_guard` (L≈3375): project root becomes `.agents`.

### 5.3 Other direct session-file readers

* `nexus/observability/session.py:241` — stop opening `log_path` bytes; use
  `store.read()`.
* `nexus/host_support/doctor.py:187` — list sessions via the manager/DB; add
  checks: DB path, schema version, `quick_check`, un-imported legacy dir,
  legacy `.nexus/` extensions present (suggest moving to `.agents/`).
* `nexus/host_support/session_archive.py:193` — search/preview read record
  tails from the DB (`ORDER BY seq DESC LIMIT n`, cap bytes by `length(body)`)
  instead of file tail bytes. Keep all caps.
* `nexus/host/facade.py:597` — already uses `sessions.store.exists`; fine.
* `nexus/config/schema.py:705` `log_file = ".nexus/logs/nexus.log"` → resolve
  relative log paths under `project_state_dir()/logs`.
* `nexus/mcp/manager.py:105` MCP stderr logs → `project_state_dir()/logs/mcp`.

### 5.4 Project extensions → `.agents/`

Introduce one helper `project_ext_roots(workspace) -> (Path('.agents'), Path('.nexus'))`
(primary, legacy) and use it everywhere a manager builds a workspace root.
Precedence: `builtin < ~/.nexus < <project>/.nexus (legacy) < <project>/.agents`.
**Writes always go to `.agents/`.**

| Area | File(s) |
| --- | --- |
| Skills | `nexus/skills/manager.py:170-182` |
| Agents (+ seeding, retire → trash) | `nexus/agents/manager.py:202,212,295,314,483-501`, `nexus/tools/builtin/task.py` docs |
| Tools (WriteTool target, template seed, search paths) | `nexus/tools/builtin/meta.py:70,547,575,595`, `nexus/ext/template.py`, `nexus/config/schema.py:674` (`[".agents/tools", ".nexus/tools", "~/.nexus/tools"]`) |
| Hooks + `hooks.toml` | `nexus/hooks/manager.py:676-696` |
| Providers | `nexus/model/providers/discovery.py:71` |
| MCP `mcp.json` (read + write + watch) | `nexus/ext/manager.py:110,1496`; writes from Settings go to `.agents/mcp.json` |
| Extension trash / stage | `nexus/ext/manager.py:597,833,2065`, `nexus/hooks/manager.py:696`, `nexus/ext/quarantine.py:1418` → `project_state_dir()/{trash,stage}` (machine state, not repo content) |
| Project settings config | `nexus/config/paths.py:workspace_settings_config_path`, `nexus/config/layers.py`, `nexus/host_support/settings_scope.py:28-33` (label `<project>/.agents`) |
| Hot-reload watch globs | `nexus/host/facade.py:1742-1747` (add `.agents/…`, keep `.nexus/…`) |
| Permission write roots | `nexus/tools/permissions.py` — confirm `.agents/` is writable like `.nexus/tools` was and that `~/.nexus/nexus.db` is **never** a tool-writable path |
| UI strings (both surfaces, per "web mirrors TUI") | `nexus/ui_support/tui_panels.py:670`, `nexus/ui_support/tui_settings.py:15,133,159,263,293,349`, `nexus/ui/web/index.html:212`, `nexus/ui/web/js/app.js:271,343`, `nexus/cli.py:56` help text |

A `.agents/` directory is the emerging cross-tool convention; Nexus must
tolerate unrelated files there (only read the documented names/globs).

## 6. Tests

New:
* `tests/test_session_db.py` — schema creation/`user_version`, file modes,
  append/read round-trip byte-identical to JSONL encoding, seq monotonic across
  two connections/processes, explicit seq collision → `SessionError`, fork
  prefix + duplicate-id refusal, WAL reader sees consistent prefix while a
  writer appends, trash/restore/purge, archive/unarchive/stale sweep, snapshot
  upsert/validate, two projects isolated in one DB, namespaces isolated,
  newer-schema refusal.
* `tests/test_session_import_legacy.py` — imports jsonl + agents + snap +
  archive + trash + v1 json; idempotent; crash-tail dropped; corrupt log left
  and reported; dir renamed not deleted; concurrent import guarded.
* `tests/test_workspace_clean.py` — run a scripted-provider turn via the
  runtime/host with `NEXUS_HOME=tmp`; assert `<workspace>/.nexus` does not
  exist afterwards and the DB contains the session.
* `.agents/` precedence tests beside peers: `test_context_skills.py`,
  `test_agents_manager.py`, hooks/providers/mcp/ext tests, settings scope.

Update:
* `tests/test_session_store.py` → targets `JsonlSessionStore` (legacy reader).
* Tests that open `*.jsonl` under a sessions dir (`test_session_manager.py`,
  `test_session_operations.py`, `test_session_migrate.py`,
  `test_host_session_archive.py`, `test_runtime.py`, `test_cli.py`,
  `test_doctor_mismatches.py`, `test_host_logs_read.py`, `test_benchmark.py`,
  `test_phase6_integration.py`, `test_browser_serve.py`, `test_tui_keys.py`,
  Playwright checks) → go through the manager/store or the DB.
* Tests asserting `.nexus/...` extension paths (~40 files) → `.agents/...`,
  plus one legacy-fallback assertion per area.
* Every test/fixture that could touch the real home must set `home=`/`NEXUS_HOME`
  to a tmp dir. Add an autouse fixture in `tests/conftest.py` that sets
  `NEXUS_HOME` to a tmp path so no test ever writes `~/.nexus/nexus.db`.

Gates: `.venv/bin/python -m pytest -q`, `ruff check nexus tests`,
`tests/test_layering.py`, `tests/test_ui_layering.py`,
`tests/test_phase3_exit.py` (line budgets — `session/` isn't budgeted, `host/`
is near its cap: add nothing to `host/` that can live in `host_support/`),
then `tests/playwright_web_check.py` and `tests/playwright_tui_check.py`.

## 7. Docs

`AGENTS.md` (conventions bullet on workspace state), `docs/core.md` (sessions,
store, locking), `docs/textual.md` / `docs/web.md` (settings scope labels,
MCP empty state), `README.md` (where things live, migration note),
`ARCHITECTURE.md` (durable log section: "JSONL log" → "append-only record
table; JSONL remains the export format"), `EXTENDING.md` (all `.nexus/<x>`
extension paths → `.agents/<x>`, legacy note).

## 8. Implementation phases (subagents)

Parallel agents share the dirty main worktree (worktree isolation would branch
from `HEAD` and lose the uncommitted work), so each agent owns a disjoint file
set; shared files (`nexus/runtime.py`, `nexus/config/paths.py`) get small
targeted `Edit`s only, and paths helpers land first.

| Phase | Owner | Files | Done when |
| --- | --- | --- | --- |
| 0 | lead | `nexus/config/paths.py` helpers (§3), `tests/conftest.py` `NEXUS_HOME` fixture | helpers importable, suite still green |
| A | agent "sessions-db" | `nexus/session/**`, `observability/session.py`, `host_support/{doctor,session_archive}.py`, runtime session/child wiring + import call, session tests, new §6 session tests | session/host/runtime tests green |
| B | agent "agents-dir" | skills/agents/hooks/providers/ext/mcp/tools-meta/config layers/settings_scope/permissions, caches/logs/stage/trash relocation, facade watch globs, UI strings (TUI + web), extension tests | extension/settings/UI tests green |
| C | lead | docs (§7), full suite, ruff, layering/budget tests, Playwright checks, fix cross-phase fallout | all gates in §6 green |

## 9. Rollout & compatibility

* First run after upgrade: DB created, legacy sessions imported, the old dirs
  renamed `*.imported-<ts>`. Users can delete `<project>/.nexus` once satisfied;
  `nexus doctor` says so.
* Downgrade: older Nexus finds no `sessions/` dir and starts empty; the renamed
  directory can be renamed back manually. Documented in README.
* Export (`/export jsonl`) still produces the historical JSONL format, so
  external tooling keeps working.

## 10. Follow-ups (out of scope)

* `.nexus-worktrees-<name>` sibling directories → `~/.nexus/projects/<key>/worktrees`.
* `nexus sessions` CLI for cross-project listing now that one DB holds all.
* FTS5 index over message text for `SessionSearch`.
* `VACUUM`/size reporting in `nexus doctor`.
