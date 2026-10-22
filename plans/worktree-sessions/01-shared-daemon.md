# 01. One daemon, many projects

Phases: **F0** (prep, no behavior change) and **F1** (shared mode, project switching).
Foundation for everything else in this directory.

## 1. Why this comes first

The user's requirement: "nexus daemon is not bound to one project. It should be able to handle
multiple projects, each project with multiple worktrees, and the ability to easily switch between
them. So it should not matter where nexus is launched from."

Today (verified on `main`):

- One daemon per resolved workspace path. Socket `~/.nexus/daemon/<project_key>.sock`, with
  `.pid`, `.lock`, `.log`, `.http` beside it (`nexus/host/daemon.py:148-186`).
  `project_key = sha256(resolved path)[:16]` (`nexus/config/paths.py:44`).
- `Daemon` owns one `Runtime` and one `HostFacade` (`daemon.py:459`, docstring "Own one runtime,
  one socket, and one lifecycle for a workspace").
- `Hello` carries no workspace (`nexus/host/transports/__init__.py:79-84`);
  `PROTOCOL_VERSION = 3` (`nexus/host/protocol.py:42`).
- The `Supervisor` (turn cap 4) is built inside each facade (`nexus/host/facade.py:161`).
- `ProjectSessionOpen(workspace, session)` returns another daemon's socket path, and the TUI
  reconnects (`protocol.py:92-140`, `docs/host.md:396-401`: "This does not implement the
  shared-daemon plan.").
- The DB already has a `projects(id, root, created_at, last_opened)` table and
  `sessions.project_id` (`nexus/session/db.py:220-229`); storage is shared across daemons.
- A linked Git worktree opened as a workspace is a **different project** (different path, key,
  daemon, settings and grants). This is the main thing worktree sessions cannot live with.

If root sessions ran in worktrees on today's model, every worktree would be its own daemon and
its own project, with its own permission grants and settings, and the merge queue (file 06)
would need cross-daemon coordination. One daemon that owns the project and all its worktrees
removes that whole class of problem.

## 2. Relation to the existing shared-daemon plan

`plans/SHARED_DAEMON_PLAN.md` (branch `worktree-shared-daemon-plan`, commit `f9ec89f`, not on
`main`; readable at `.claude/worktrees/shared-daemon-plan/plans/SHARED_DAEMON_PLAN.md`) already
audits the process-global hazards of hosting several runtimes in one process. **Adopt it
wholesale for its audit and fixes**, with these changes:

| Shared-daemon plan says | This plan changes it to | Why |
| --- | --- | --- |
| Unit is the **workspace** (resolved path); a linked worktree is another runtime | Unit is the **project** (§3); a project's worktrees are session roots in the project's runtime (file 02) | D4: one project, many worktrees, shared settings and grants |
| `Hello.workspace` binds a connection to one workspace for its life | `Hello.focus` sets the initial project; `ProjectFocus` changes it; session-scoped commands route by the session's project (§5) | "Easily switch between them" |
| Project switcher is a non-goal (§9 "Later") | Project switcher is in scope (file 08) | User requirement |
| Turn cap replaced by process and request slots, fair by workspace then session | Same, fair by **project** then session | Same reasoning |
| Web gets `/w/<project_key>/` routing | Keep minimal: web binds one project per tab, no switcher (web is to be deprecated) | AGENTS.md: no new web features |

Everything in that plan's §3.1 (blockers B1–B6), §3.2 (C1–C10) and §3.3 (checked-safe list)
carries over; B4 and B6 are reshaped as above. This file does not repeat the audit; it states
only what is new or different.

`feat/shared-daemon` (2 commits, `ddc7817`, `e1ba1ea`, 2026-10-02: "isolate runtime and shell
job environments" and a credentials/voice fix) diverged from `main` 36 files ago. Recommended:
**re-implement F0 on `main`**, reading those commits as reference; do not rebase them blindly.

## 3. Project identity

A **project** is the unit of settings, permission grants, extensions, sessions and the merge
queue.

| Folder kind | Project root | Project key |
| --- | --- | --- |
| Git main checkout | the checkout's top level | `sha256(resolved root)[:16]` (unchanged from today) |
| Git linked worktree | the **main checkout** that owns it (`git rev-parse --git-common-dir` → its parent; `host_support/git_head.py` already finds `main_root` without a subprocess) | the main checkout's key |
| Subdirectory of a repo | the repo top level | the top level's key |
| Non-git folder | the folder itself | `sha256(resolved folder)[:16]` |
| Bare repo / `GIT_DIR` tricks / submodule checkout | *not verified*; a submodule checkout is treated as its own repo (its own `.git` file points into the superproject's modules dir, so `git_head` would report it as a worktree; must special-case `modules/` gitdirs) | — |

Consequences:

- For every main checkout and non-git folder, the key is **the same as today**, so existing
  sessions, state dirs (`~/.nexus/projects/<key>/`) and DB rows keep working.
- Launching from a subdirectory changes today's behavior (today the subdirectory would be its own
  workspace). The launch directory becomes the **initial shell cwd** of a new session (it must
  be inside the session root), not a project. *Decision needed* (§10 Q1).
- Sessions recorded today under a manually created linked worktree (`nexus --workspace <wt>`)
  have their own project key. Migration (§8) re-parents them.

Implementation: `nexus/host_support/project_identity.py` (new): `resolve_project(path) ->
ProjectIdentity{key, root, kind: "git"|"plain", launch_root, launch_worktree: str | None}`,
subprocess-free where possible (reuse `git_head`), bounded (walk ≤ 64 levels, as `git_head`).

## 4. Process model

```
nexus chat (cwd=/a/wt-2) ─Hello{v4, focus=/a/wt-2, env}─┐
nexus chat (cwd=/b)      ─Hello{v4, focus=/b, env}──────┤
nexus run  (cwd=/a)      ─Hello{v4, focus=/a, env}──────┤
                                                        ▼
             Daemon (one per NEXUS_HOME + install identity)
             ├─ ProjectRegistry {project_key → ProjectHost}
             │    ProjectHost = Runtime(project_root, environ) + HostFacade
             │                  + Presence + idle timer + log ring
             │                  + SessionRoots (file 02) + MergeQueues (file 06)
             ├─ Supervisor (fair by project → session; ceiling only)
             ├─ ResourceLimits (process slots, per-provider request slots)
             ├─ UDS listener (one socket) · HTTP listener (one port)
             └─ Router: connection focus + session → project index
```

- **`ProjectHost`** (new, `nexus/host/projects.py`): one runtime per project, built lazily on
  first use, ref-counted by connections and subscriptions, evicted when idle (shared-daemon plan
  C6). `acquire(identity, env, client) -> ProjectHost`.
- **`Daemon`** loses `workspace`, gains `projects: ProjectRegistry`. Daemon-level commands
  (`Health`, `Shutdown`, `UpdateStatus`, `ProjectList`, `ProjectOpen`) answer for the process.
- **Socket**: `~/.nexus/daemon/shared-v<PROTOCOL_VERSION>-<sha8(sys.prefix)>.sock` (+ `.pid`,
  `.lock`, `.log`, `.http`), so two installs (editable checkout vs `uv tool`) run two daemons
  (shared-daemon plan B5). The socket name no longer depends on any path.
- **Spawn** with `cwd=nexus_home()` (B2). The first client to load a project supplies its env
  (B1); Doctor shows "environment captured from <client> at <time>" (C7).
- **Bounds**: at most 32 loaded projects (LRU eviction of idle ones, else a clear refusal);
  Hello env ≤ 1024 vars and ≤ 256 KiB.

## 5. Routing and switching

The connection has a **focus project**, not a fixed binding.

| Command kind | Routed to |
| --- | --- |
| Session-scoped (`SessionOpen`, `SessionEnqueue`, `SessionSubscribe`, `SessionCancel`, `SessionLand*`, …) | the project that owns the session id (DB lookup `sessions.project_id`, cached in a bounded map); refuse if the project root is missing |
| Project-scoped (settings, permissions, Doctor, `/worktrees`, `LogsRead`, `GitStatus` without a session) | an explicit `project` field when present, else the connection's focus |
| Daemon-scoped (`Health`, `Shutdown`, `ProjectList`, `ProjectOpen`) | the daemon |

Protocol v4 changes (`nexus/host/protocol.py`, `transports/__init__.py`):

```
Hello{version: 4, client_id, client, focus: str  # absolute path, required
      env: dict[str,str] | None}                 # bounded; UDS only, never echoed
Welcome{..., focus: ProjectRef}                   # resolved identity
ProjectRef{key, root, name, kind: "git"|"plain"}
ProjectFocus{project: str}  -> ProjectFocusResult{project: ProjectRef}
ProjectOpen{path: str}      -> ProjectOpenResult{project: ProjectRef, created: bool}
ProjectList{limit=100}      -> ProjectListResult{projects: list[ProjectRow], truncated}
ProjectRow{project: ProjectRef, loaded: bool, exists: bool, sessions: int,
           running: int, queued: int, worktrees: int, behind: int,
           last_activity: float}
```

- Project-scoped commands gain an optional `project: str = ""` (key). Empty means focus.
- `ProjectFocus` does not cancel subscriptions; a client may watch sessions in several projects
  at once (the dashboard does). It only changes the default for project-scoped commands.
- `ProjectSessionOpen` stops returning another daemon's socket: it focuses the session's project
  and returns the session handle. `socket_path` is kept empty for one release, then removed.
- Events delivered to a client carry their project key so the TUI can badge activity in other
  projects (file 08).

**Launch semantics**: `nexus chat` in any folder → ensure the daemon → `Hello{focus=cwd}` →
resolve identity → open the focus project. If cwd is a linked worktree that Nexus owns, the TUI
opens the session bound to that worktree (file 03 §2 records it); if it is a user-made linked
worktree, it focuses the main project and offers "New session in this worktree" (file 02 §5,
external roots). `--workspace PATH` keeps working as "focus this path".

## 6. Scheduling and limits across projects

Adopt shared-daemon plan §4.1: process slots (`[daemon] max_processes`, default 16) and
per-provider request slots (`[daemon] max_requests_per_provider`, default 6), round-robin by
project then session; the turn cap becomes a ceiling of 32. Worktree Git subprocesses
(file 03), setup commands (file 07) and land checks (file 06) take process slots like shell
jobs. A waiting step shows `waiting for a process slot` in its row.

## 7. Lifecycle and CLI

| Command | Behavior |
| --- | --- |
| `nexus daemon status` | process line, then one row per loaded project (sessions running/queued, worktrees, loaded since) |
| `nexus daemon stop` | unload the focus project (refuse with a running turn unless `--force`); `--all` stops the process |
| `nexus daemon restart` / `nexus update` | list projects with running or queued turns; refuse without `--force` |
| `nexus daemon reload` | re-create the focus project's runtime with the caller's env (no turn running) |
| `nexus daemon logs` | focus project; `--all` for everything |
| `nexus projects` (new) | list known projects; `nexus projects open PATH` registers one |

Idle: per-project eviction after `idle_timeout` with no viewer, no turn, no queued turn and no
land in progress; daemon exit when no project is loaded for `idle_timeout`.

## 8. Migration

1. v4 clients refuse v3 daemons (`VersionMismatch`); `nexus update` already stops old daemons;
   stale `<hash>.sock` files are reclaimed by the existing stale-socket path.
2. Escape hatch: `NEXUS_DAEMON=per-workspace` for one minor release after the default flips
   (shared-daemon plan C3); in that mode worktree sessions are unavailable and say so.
3. **Re-parenting linked-worktree projects**: at first load, for every `projects` row whose root
   is a linked worktree of a known main checkout (via `git_head`), record an alias
   (`project_aliases(alias_id, project_id, root, created_at)`, new table) so its sessions list
   under the main project with `root = <worktree>` (external root, file 02 §5). Rows are not
   rewritten; the alias is reversible. Per-project state dirs of the alias are left in place and
   listed in Doctor.
4. No change to session records.

## 9. Failure modes

| Failure | Behavior |
| --- | --- |
| One project's runtime fails to build (bad config, extension error) | that project shows the error in its focus view; other projects unaffected; retry via `nexus daemon reload` |
| Event-loop stall from one project's extension | loop-lag watchdog logs and names the project (C3); all projects stall (accepted, documented in `docs/security.md`) |
| Daemon crash | every project's turns stop; durable logs replay on restart (existing recovery); merge queue recovery (file 07 §3) |
| Project root deleted while loaded | project marked `exists=false`; sessions read-only; no Git operations |
| Two installs | two daemons by socket name; a project open in both shows the existing session/worktree locks (flock-based, cross-process safe) |

## 10. Open questions

1. **Subdirectory launch**: project = repo top level (recommended; matches Git and the worktree
   model) vs today's "the folder is the workspace". The former changes which `.agents/` is read
   when users run Nexus in a monorepo package folder. Recommended: top level, with
   `[project] root = "subdir"` opt-out in a `.agents/nexus.toml` found at the launch folder.
2. **Env per project vs per session**: worktree sessions inherit the project's captured env.
   Recommended: yes; `.envrc` differences between worktrees are out of scope.
3. **Non-git folders inside a repo's ignored dirs**: treated as the repo (top level wins).

## 11. Tests (F0, F1)

From the shared-daemon plan §6 (env isolation, cwd independence, module namespacing, resource
limits, credentials, eviction, CLI semantics, static guards), plus:

- `tests/test_project_identity.py`: main checkout, linked worktree (→ main), subdirectory,
  non-git folder, submodule, missing `.git` target; bounded walk.
- `tests/test_host_daemon_projects.py`: two projects in one daemon; `ProjectFocus` changes the
  default without dropping subscriptions; session commands route by session; a project-scoped
  command with an explicit key reaches only that project; events carry the project key.
- `tests/test_project_aliases.py`: migration of a linked-worktree project row.
- Native PTY journey: launch in project A, switch to B from the picker, run a turn in each,
  both visible in the dashboard.

## 12. Docs

`docs/host.md` (daemon, routing, commands), `docs/architecture.md` (invariant "one daemon per
workspace" replaced), `docs/decisions.md` (new row replacing per-workspace daemon and the
`:263` cross-project entry), `docs/config.md` (`[daemon]`, env sources), `docs/security.md`
(C4 trust widening), `docs/cli.md` (`daemon`, `projects`), `docs/sessions.md` (project identity,
aliases), `docs/module-map.md` (`host/projects.py`, `host_support/project_identity.py`),
`AGENTS.md` intro sentence ("A per-workspace daemon owns sessions and turns").
