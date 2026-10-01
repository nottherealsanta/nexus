# One daemon for many projects

Status: proposed (2026-10-01). Not implemented. Scope: `nexus/host/`
(daemon, transports, web routes), `nexus/runtime.py` lifecycle, the
process-global seams listed in §3, the CLI `daemon` subcommands, and the docs
that describe "one daemon per workspace" (`docs/host.md`,
`docs/architecture.md`, `docs/decisions.md`, `docs/config.md`,
`docs/security.md`, `docs/web.md`, `docs/cli.md`).

Implementation branch: `feat/shared-daemon` in its own worktree (§8).

## 1. Goal

Today every workspace gets its own `python -m nexus.host.daemon` process.
With five projects open you have five interpreters, five copies of every
import, five keychain caches, five voice models when voice is on, and five idle
timers. The goal is **one daemon per Nexus home** that hosts many workspaces:

1. `nexus chat` / `nexus run` / `nexus web` in any project attach to the same
   daemon process. The first one starts it.
2. Every workspace keeps exactly today's behavior: its own config, extensions,
   permissions, sessions, approvals, worktrees, MCP servers and hot reload.
   Nothing one project configures leaks into another.
3. The user can still see and control each project separately: status, stop,
   logs and Doctor are reported per workspace.

Non-goals for this plan: a project switcher inside one TUI or web window
(possible later, §9). Remote or multi-user daemons. Changes to the session
storage format; SQLite is already shared across projects.

## 2. What exists today (facts from the code)

| Area | Today | Where |
| --- | --- | --- |
| Addressing | socket `~/.nexus/daemon/<project_key>.sock` (+ `.pid`, `.lock`, `.log`, `.http`, `.err`); `project_key` = 16 hex of `sha256(resolved path)` | `host/daemon.py` `default_socket_path`, `config/paths.py` |
| Ownership | `Daemon(workspace)` owns one `Runtime` and one `HostFacade(runtime)`; `flock` on `.lock` stops a second daemon for the same workspace | `host/daemon.py:421`, `_acquire_lock` |
| Handshake | `Hello(version, client_id, client)` has **no workspace**; `Welcome` reports the daemon's one workspace | `host/transports/__init__.py:79` |
| Spawn | `ensure_daemon` spawns with `cwd=workspace` and `env={**os.environ, ...}` of the **calling client** | `host/daemon.py:1005` `_default_spawn` |
| Env use | config, providers, MCP and voice read `self._environ or os.environ`; bash jobs always start from `os.environ.copy()` | `runtime.py`, `tools/builtin/_jobs.py:293`, `config/__init__.py:82` |
| Turn cap | `Supervisor(max_concurrent=4)` per daemon, round-robin across sessions | `host/supervisor.py`, `host/facade.py:118` |
| Idle | exit after 300 s with no viewer, no running and no queued turn | `host/daemon.py:769` |
| Web | one loopback HTTP listener per daemon, random port; `BrowserRoutes(workspace=...)`; cookie `nexus_web`, `Path=/` | `host/web.py`, `transports/http_sse.py:311` |
| Hygiene | `nexus update` lists `~/.nexus/daemon/*.sock` and stops every daemon; warns on version skew per daemon | `host_support/install.py` |
| Storage | one shared `~/.nexus/nexus.db` (WAL, `BEGIN IMMEDIATE`), already built for many daemons | `session/db.py`, `docs/sessions.md` |
| Dev mode | separate `NEXUS_HOME` → separate daemon dir | `cli.py:128` |

The facade is already the right unit: every command goes through
`HostFacade.handle` over one `Runtime`. A multi-project daemon is mostly "a map
of workspace → facade, plus picking the right one per connection." The risk is
in process-global state, covered next.

## 3. Unintended consequences: audit

Each row: what changes when two runtimes share one process, how bad it is,
and the fix. **Blocker** means the shared daemon is incorrect without the fix.

### 3.1 Blockers

| # | Consequence | Why | Fix |
| --- | --- | --- | --- |
| B1 | **Wrong environment for bash and providers.** Project B's `bash` tool, MCP servers and env-keyed providers run with project A's env (direnv/`.envrc`, activated venv, `PATH`, `AWS_PROFILE`, API keys exported per project). | The daemon's `os.environ` is the env of whichever client spawned it. Today that is always a shell in the same project. `_jobs.py` copies `os.environ`; `Runtime(environ=None)` falls back to `os.environ`. | The client sends its env snapshot in `Hello` (bounded: ≤ 1024 vars, ≤ 256 KiB). The daemon passes it as `Runtime(environ=...)` when it creates that workspace's runtime. Every `os.environ` fallback reachable from a runtime is replaced by the runtime's environ: `_jobs.py` takes a base env from `ToolContext`, plus `agents/worktrees.py:264`, `hooks/manager.py:1450`, `mcp/client.py`, `voice`, every provider. A test asserts that no module under `tools/`, `mcp/`, `hooks/`, `agents/`, `model/providers/` reads `os.environ` directly. |
| B2 | **Process cwd is a random project.** | Spawn sets `cwd=workspace`. Fallbacks use it: `ext/quarantine.py:1127` (`Path.cwd() / path`), `:1418` (`os.getcwd()/.nexus/stage`), `model/providers/opencode.py:908`, and any of the ~35 `subprocess`/`create_subprocess_*` calls that omit `cwd=`. | Spawn the shared daemon with `cwd=nexus_home()`. Make `workspace` a required argument at the three fallbacks. Audit the subprocess calls; each one passes an explicit `cwd`. A test fails if `os.getcwd`/`Path.cwd` appears outside `cli.py`. |
| B3 | **Extension modules collide in `sys.modules`.** A user-scope tool or hook in `~/.nexus/tools/` loads in every runtime; the second runtime's load fails with `module … is already loaded`. | `tools/loader.py:96` `module_name_for(source_identity, generation)`: the same absolute path plus a per-runtime generation counter gives the same name. The same applies to `hooks/manager.py:367`. Provider files are content-addressed (`discovery.py:319` `nexus_hot_provider_<sha16>`), so identical provider files in two projects share one module object, and one runtime's teardown pops the other's. `tools/loader.py:616` `_install_extra` also registers modules globally. | Prefix every dynamic module name with the runtime's `project_key`, for example `nexus_ext_<project_key>_<label>_<gen>`. Make `_install_extra` refuse an existing name that a different runtime owns. A test loads the same user-scope tool into two runtimes in one process. |
| B4 | **Handshake does not name a workspace.** | `Hello` has no workspace field. Every command assumes the daemon's single runtime. | `Hello.workspace` (resolved absolute path) becomes required. `PROTOCOL_VERSION` 3 → 4. The connection binds once to one `HostFacade`; it cannot switch workspaces mid-connection. `Welcome.workspace` echoes the bound workspace. |
| B5 | **Version skew between installs.** Today an editable checkout (the nexus repo itself) and a `uv tool` install can each run their own daemon for different projects. Shared, the second client either gets `VersionMismatch` or silently runs on the other install's code. | One socket for all projects. | Key the shared socket by protocol version and install identity: `~/.nexus/daemon/shared-v<PROTOCOL_VERSION>-<sha8(sys.prefix)>.sock`. Two installs then run two daemons, as two projects do today. `install_report` keeps its skew warning. |
| B6 | **Web cookie collision.** Two browser tabs for two projects on the same origin overwrite each other's `nexus_web` cookie (`Path=/`). | One listener now serves every workspace. | Every browser route moves under `/w/<project_key>/…`. The cookie becomes `nexus_web_<project_key>` with `Path=/w/<project_key>/`. A ticket is minted for one workspace and only opens that path. `localStorage` keys are already workspace-scoped (`app.js` `SEEN_KEY`, `preferences.js`), so they need no change. |

### 3.2 Behavior changes to decide (not bugs, but visible)

| # | Consequence | Recommendation |
| --- | --- | --- |
| C1 | **Turn cap becomes machine-wide.** `max_concurrent=4` per daemon was effectively 4 × N projects. Shared, five projects compete for 4 slots. | Keep one global cap; the "fork bomb" rationale is per machine. Raise the default to 8 and make it a user-scope setting (`[daemon] max_concurrent_turns`). Round-robin fairness goes two levels deep (workspace, then session) so one busy project cannot starve another. Show queued-for-capacity turns in the status line (`waiting for a slot`). |
| C2 | **Lifecycle commands hit every project.** `nexus daemon stop` / `restart` / `nexus update` now interrupt turns in other projects. | `nexus daemon stop --workspace` (the default, run in a project) **unloads that workspace's runtime only**. `nexus daemon stop --all` stops the process. `restart` and `update` list every workspace with a running or queued turn and refuse without `--force`. |
| C3 | **Blast radius.** A crash, `MemoryError`, or a blocking call on the event loop (an `async` extension tool that does sync I/O, a slow `flock` in a thread) stalls **every** project instead of one. Running turns in all projects die with the process. | Accept, and reduce it. Add a loop-lag watchdog (log and Doctor when the loop stalls > 1 s, naming the workspace whose task was running). Keep `NEXUS_DAEMON=per-workspace` as a supported escape hatch for one release after the default flips (§7). Recovery already exists: turns are durable and replay reproduces state. |
| C4 | **Trust boundary widens.** A cloned repo's `.agents/tools/*.py`, hooks or providers run in-process with every other project's in-memory state: permission engines, open sessions, cached keychain secrets. Today it shares a process with only its own project. | It already runs as the same uid with full file access, so this is a modest increase. Document it in `docs/security.md`. Keep quarantine as is. Treat B3's per-runtime module namespace as hygiene, not isolation. Long term: run project extensions out of process (§9). |
| C5 | **Logs mix projects.** One `.log` file and one in-memory diagnostics ring (`observability/daemon.py`, ≤ 512 entries) now cover all workspaces. `LogsRead` in project A could show B's lines, and B's noise could evict A's. | Tag every log line and diagnostic entry with `project_key`. `LogsRead` and `nexus daemon logs` filter to the bound workspace by default (`--all` for everything). Keep one ring per workspace with the same bounds, plus a small daemon-level ring. |
| C6 | **Idle shutdown semantics.** One busy project keeps the daemon alive. Idle projects keep MCP servers, file watchers (500 ms polling) and voice models resident. | Two timers. Per-workspace runtime eviction after `idle_timeout` with no viewer and no turn: close MCP, watchers and voice; drop the runtime. Daemon exit when no runtime is loaded for `idle_timeout`. |
| C7 | **Env is captured once per workspace.** With B1 the runtime keeps the env of the first client that attached; a later `export FOO=…` in another terminal is not seen. Today the same is true per daemon. | Same as today, but visible: Doctor shows "environment captured from <client> at <time>". `nexus daemon reload` (per workspace) re-creates the runtime with the caller's env when no turn is running. |
| C8 | **`NEXUS_*` overrides at spawn time** (`NEXUS_MODEL`, `NEXUS_SANDBOX`, `NEXUS_HTTP`, `NEXUS_VOICE=off`, …) are daemon-wide today. | Per-workspace overrides (`NEXUS_MODEL`, `NEXUS_SANDBOX`, `NEXUS_VOICE`, `NEXUS_*_FILE`) come from the Hello env via B1. Daemon-wide ones (`NEXUS_HTTP*`, `NEXUS_HOME`, `NEXUS_DEV`) are read only from the daemon process env. List both sets in `docs/config.md`. |
| C9 | **Voice memory.** Each runtime has its own `VoiceManager`, so two projects with voice loaded hold two models. | Phase 6: share one process-level engine keyed by `(model, revision, device)` with a refcount. Per-workspace config still decides enablement. |
| C10 | **Startup cost moves.** The first attach to a new project costs runtime construction (config, registry, extensions, MCP connect) inside the shared daemon, not a process spawn. A slow MCP connect must not block other projects' commands. | Construct runtimes in a task. The connection awaits its own runtime's readiness with a bound (10 s, as `ensure_daemon` today); other connections are unaffected. |

### 3.3 Checked and safe (no change needed)

- **SQLite.** Already shared by many daemons. One process with many runtimes
  is a subset of that. Each runtime may keep its own connection; pooling is an
  optimization, not required.
- **Session and trash locks** use `fcntl.flock` on separate file
  descriptors (`session/lock.py`). `flock` locks belong to the open file
  description, so two runtimes in one process still exclude each other.
  (POSIX `lockf`/`fcntl` record locks would not; none are used.)
- **Worktree locks** (`agents/worktrees.py:370`, `worktree_integrate.py:726`)
  are blocking `flock`s, but they are held only inside synchronous blocks with
  no `await`, so they cannot deadlock the loop. At worst a sibling's
  off-thread holder causes a short stall (covered by the C3 watchdog).
- **Job, todo and skill stores.** `Runtime` builds its own `JobRegistry`,
  `TodoStore` and `SkillActivationLog` (`runtime.py:2052`, `:2055`, `:2237`).
  The process-wide defaults in `_jobs.py`/`todo.py`/`skill.py` are only
  fallbacks. Add a test that `Runtime.aclose` never calls
  `close_default_registry()` or `set_default_*`.
- **Keychain cache** (`auth/store.py` `_CACHE`) keys by item and is
  invalidated by a stamp file. Sharing it is a **benefit**: fewer macOS
  keychain prompts.
- **Signals.** Only the daemon installs SIGINT/SIGTERM handlers.
- **Permissions and path guards** are per-runtime (`PermissionEngine(workspace=…)`),
  and `nexus.db` stays tool-inaccessible.
- **Dev mode** uses its own `NEXUS_HOME`, so it still gets its own daemon.
- **Subagent worktrees** opened directly as a workspace become another runtime
  in the same daemon, which is the same as today's extra daemon.

### 3.4 Benefits

One interpreter and import set. A new project attaches in milliseconds instead
of a spawn plus imports (~1–2 s, not measured). One keychain cache, one update
check, one HTTP port for the browser, a machine-wide turn cap that actually
bounds the machine, and one place to see everything running (`nexus daemon status`).

## 4. Design

```
client (cwd=/p/a) ──Hello{v4, workspace=/p/a, env}──┐
client (cwd=/p/b) ──Hello{v4, workspace=/p/b, env}──┤
browser /w/<key_b>/… ──HTTP(ticket→key_b)───────────┤
                                                    ▼
                         Daemon (one per NEXUS_HOME + install)
                         ├─ Supervisor (global cap, fair by workspace→session)
                         ├─ WorkspaceHost registry  {project_key → WorkspaceHost}
                         │    WorkspaceHost = Runtime(workspace, environ) + HostFacade
                         │                    + Presence + idle timer + log ring
                         ├─ UDS listener   (one socket)
                         └─ HTTP listener  (one port, /w/<project_key>/…)
```

- **`WorkspaceHost`** (new, `nexus/host/workspaces.py`): owns one runtime,
  its facade, readiness, last activity, the captured env and its metadata.
  `async acquire(workspace, env, client) -> HostFacade` creates it on first
  use or returns the live one. `release()` is reference counted by attached
  connections and subscriptions. `aclose()` drains.
- **`Daemon`** loses its `workspace` attribute and gains `workspaces:
  WorkspaceRegistry`. `_ClientConnection` stores its bound `WorkspaceHost`
  after `Hello`, and every `CommandFrame` is dispatched to that facade.
  Daemon-level commands (`Health`, `Shutdown`, `UpdateStatus`) answer for the
  process. `Doctor` and `LogsRead` answer for the bound workspace.
- **`Supervisor`** moves from `HostFacade.__init__` to the daemon and is
  injected into each facade (`HostFacade(runtime, supervisor=…)`). Its queues
  are keyed `(project_key, session_id)`.
- **Health** gains `workspaces: [{workspace, project_key, viewers, running,
  queued, loaded_at, last_activity, env_from}]`, bounded to 64 entries.
- **Discovery files:** `shared-v4-<install>.sock`, `.pid`, `.lock`, `.log`,
  `.http` in the same `0700` dir. The `.pid` file lists the loaded
  workspaces for `nexus daemon status` without a connection.
- **Bounds:** at most 32 loaded workspaces (a new one evicts the
  least-recently-used idle runtime, or is refused with a clear error if none is
  idle). Hello env ≤ 1024 vars, ≤ 256 KiB. Existing per-connection limits are
  unchanged.
- **Security:** the env snapshot travels **inward only** over the `0600` UDS
  from the same uid. It is never echoed, logged or returned. Doctor shows only
  variable *names* that Nexus reads, with redaction, as today. The browser never
  sends an env; a browser can only open a workspace that already has a loaded
  runtime or that `nexus web` (a UDS client) named in its ticket.

## 5. CLI and surface changes

| Command | New behavior |
| --- | --- |
| `nexus daemon status` | process line (pid, version, uptime, turn cap usage) and then one row per loaded workspace; `--workspace` narrows |
| `nexus daemon stop` | unloads the current workspace's runtime (refuses with a running turn unless `--force`); `--all` stops the process |
| `nexus daemon restart` | restarts the process; lists other workspaces with running or queued turns and needs `--force` |
| `nexus daemon reload` (new) | re-creates the current workspace's runtime with the caller's env |
| `nexus daemon logs` | filters to the current workspace; `--all` |
| `nexus update` | stops the shared daemon(s); same `--force` rule |
| `nexus web` | `WebLaunch` returns `http://127.0.0.1:<port>/w/<project_key>/#ticket=…` |
| TUI and web top bar | unchanged; the workspace shown is the bound one. Status may say `waiting for a slot` (C1) on both surfaces |

`docs/surfaces.md` parity: the slot-wait status and the Doctor "environment
captured" row land in both the TUI and the web client in the same change.

## 6. Tests

New or changed, next to their peers:

- `tests/test_host_daemon_shared.py`: two workspaces in one `Daemon`; separate
  sessions, config and permissions; a command on connection A never reaches
  B's runtime; `Hello` without a workspace is rejected; v3 clients are
  rejected with `VersionMismatch`.
- Env isolation: a bash tool in A sees `FOO=a` and in B sees `FOO=b` from
  their Hello envs; the daemon's own `os.environ` is never used (B1).
- Cwd independence: spawn with `cwd=/`; every tool, extension stage and
  provider still resolves paths against the workspace (B2).
- Module namespacing: the same `~/.nexus/tools/x.py` and the same provider
  file load in two runtimes, hot-reload independently, and unload without
  breaking the other (B3).
- Supervisor fairness across workspaces; global cap; queued turns survive the
  other workspace's cancel (C1).
- Per-workspace eviction and daemon idle exit; never evict with a turn in
  flight or a viewer attached (C6).
- `nexus daemon stop` / `--all` / `--force` semantics (C2).
- Web: two tickets for two workspaces in one browser; cookies are scoped by
  path; a ticket for A cannot open B's routes. Extend
  `tests/test_web_transport.py` and `tests/playwright_web_check.py` (B6).
- Logs and diagnostics filter by workspace (C5).
- Static guards: no `os.environ`/`os.getcwd`/`Path.cwd` in runtime-reachable
  modules (allow-list `cli.py`, `config/paths.py` for `NEXUS_HOME`,
  `host/daemon.py`).
- Existing `tests/test_host_*`, `tests/test_layering.py`,
  `tests/test_ui_layering.py` and the full suite stay green in both modes
  while the flag exists.

## 7. Phases

Each phase is a separate PR on top of `feat/shared-daemon` (or merged to `main`
behind the flag), with green tests and updated docs.

0. **Prep, no behavior change.** Fix B1 (thread `environ` everywhere; `_jobs`
   base env from context), B2 (no cwd reliance), B3 (namespaced module names),
   and C5 tagging. These are correct even with per-workspace daemons and
   reduce the risk of later phases.
1. **`WorkspaceHost` refactor.** Introduce the registry and inject the
   `Supervisor` with exactly one workspace. Daemon behavior and socket path
   are unchanged.
2. **Protocol v4 and shared mode behind a flag.** `Hello.workspace` and
   `Hello.env`; `NEXUS_DAEMON=shared|per-workspace` (default `per-workspace`).
   Shared socket naming (B5), `ensure_daemon` targets the shared socket in
   shared mode, spawn with `cwd=nexus_home()`.
3. **Lifecycle.** Lazy creation with readiness bounds (C10), per-workspace
   eviction and daemon idle (C6), the LRU cap of 32, two-level fairness and the
   configurable cap (C1), loop-lag watchdog (C3).
4. **CLI, Doctor, install.** §5 commands, Health/Doctor rows, `nexus update`
   hygiene for both socket kinds, `nexus daemon reload` (C7).
5. **Web.** `/w/<project_key>/` routing, per-workspace cookie and ticket (B6),
   playwright check.
6. **Flip the default** to `shared`. Keep `per-workspace` for one minor
   release. Update `docs/decisions.md` (replace "One daemon per workspace owns
   the runtime" with the new decision and its reasons), `docs/host.md`,
   `docs/architecture.md`, `docs/config.md`, `docs/security.md` (C4),
   `docs/web.md`, `docs/cli.md`. This flips user-visible behavior, so it needs
   explicit approval for a minor version bump (AGENTS.md).
7. **Optional.** Shared voice engine (C9). Remove the per-workspace mode.

Migration: a v4 client never talks to a v3 per-workspace daemon (version
mismatch), so after an upgrade `nexus update` already stops the old daemons.
Old `<hash>.sock` files are reclaimed by the existing stale-socket path. No
data migration is needed because sessions are already in the shared DB.

## 8. Working in a branch / worktree

```sh
git fetch origin
git worktree add ../nexus-shared-daemon -b feat/shared-daemon origin/main
cd ../nexus-shared-daemon
uv sync --extra dev
git config core.hooksPath scripts/hooks
.venv/bin/python -m pytest -q          # baseline before phase 0
```

- Use a **separate `NEXUS_HOME`** while developing
  (`export NEXUS_HOME=$PWD/.nexus-dev-home`) so the shared daemon under test
  never touches the per-workspace daemons your main checkout is running.
  Different install prefixes also get different sockets (B5).
- One Conventional Commit per phase (`refactor:` for 0–1, `feat:` for 2–5,
  `feat!:` or a documented minor bump for 6).
- Open a draft PR from `feat/shared-daemon` after phase 1 so CI runs on every
  phase.

## 9. Later

- A project switcher in the TUI and web sidebar, now that one daemon knows
  every loaded workspace (needs a `WorkspaceList` command and a per-surface
  design in `docs/surfaces.md`).
- Out-of-process project extensions (C4) for real isolation between projects.
- A shared SQLite connection pool and shared model catalogue refresh.

## 10. Open questions

1. Default turn cap: 8 machine-wide, or `4 × min(loaded workspaces, 2)`?
2. Should `nexus daemon stop` with no flags unload one workspace (proposed)
   or keep today's "stop the process" meaning, with `unload` as a new verb?
3. Env refresh: is "first client wins, `reload` to refresh" (today's
   semantics) enough, or should a later client whose env differs see a notice?
