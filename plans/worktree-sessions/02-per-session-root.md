# 02. Per-session root

Phase: **F2**. Depends on F1 (file 01). No user-visible change on its own: every existing
session's root is its project root.

## 1. Contract

A session has a **project** (file 01 §3: settings, grants, extensions, queue) and a **root**
(the directory its tools work in). Today they are the same path. After F2:

- `root` is either the project root (`kind = "main"`), a Nexus-owned worktree
  (`kind = "worktree"`, file 03), or a user-made linked worktree of the same repository
  (`kind = "external"`, §5).
- Everything that touches files, runs processes or reads Git **for a session** resolves against
  the session root. Everything that is **configuration or authority** resolves against the
  project.
- The root is a durable fact of the session log; replay reproduces it.

## 2. What exists to build on

- `ToolContext.workspace: Path` (`nexus/tools/spec.py:645`) is already the per-call root; tools
  never see a `Runtime`.
- Subagents already run with a different root: the runner builds a `ChildSpec` with
  `workspace=child_workspace`, `worktree=…`, the parent's `permissions` and `grants`
  (`nexus/agents/runner.py:2127-2171`, `:2612-2627`), and `PathGuard.for_worktree` rebases write
  roots (`nexus/tools/permissions.py:841-901`). F2 generalises this from "child of a session" to
  "any session".
- Shell cwd is per session already (`docs/tools.md:252-264`; stored under
  `~/.nexus/projects/<key>/shell/<session>/cwd`), with a `cwd_check` hook in the job
  (`nexus/tools/builtin/_jobs.py:293-317`).

## 3. Data model

```python
@dataclass(frozen=True)
class SessionRoot:            # nexus/session/root.py (new)
    kind: Literal["main", "worktree", "external"]
    path: Path                # resolved; == project root for "main"
    project_key: str
    branch: str = ""          # "" for main/plain; nexus/session/<slug> for worktree
    base_ref: str = ""        # target branch name, worktree only
    base_sha: str = ""
    worktree_id: str = ""     # service-root record id, worktree only
```

Durable event `session.root_set {kind, path_display, branch, base_ref, base_sha, worktree_id}`
(bounded; `path_display` is relative to the project's parent or `~`-prefixed, never a raw
absolute path to clients; the daemon keeps the absolute path in its records). Appended:

- implicitly never for `main` (absence = main; no migration needed),
- by file 03 when a worktree is created or a session is promoted,
- by §5 when a session opens on an external worktree.

Reducer: `ConversationView.root` (kind, display path, branch, base) in `nexus/view/reduce.py`;
documented in `docs/events-and-view.md`.

The daemon keeps an in-memory index `session_id → SessionRoot` per `ProjectHost`, rebuilt from
logs on load (only the last `session.root_set` per session matters; bounded by the session
count already loaded).

## 4. What resolves against what

| Concern | Today | After F2 | Notes |
| --- | --- | --- | --- |
| Tool `workspace` (`ToolContext`) | daemon workspace | **session root** | `runtime.py:447` `ToolContext(` |
| `PathGuard` write roots | workspace | **session root**; the project root and other worktrees are **read-only** for worktree sessions (writes refused with "this session works in worktree X; the main checkout is read-only here") | deny roots carry over (`for_worktree` already does this) |
| Shell job cwd, `cd` bounds | workspace | **session root** | `!` composer too (`docs/tools.md`: ignores session cwd today; it should start at the session root) |
| `GitStatus`, `GitDiff`, `FilePreview`, `DirectoryList` | workspace | **session root** when a `session` is given; project root otherwise | host commands gain `session: str = ""` |
| `AGENTS.md` / project instructions in context | workspace | **session root** (the worktree's committed copy, including edits the session made) | frozen per turn as today |
| `MEMORY.md` | workspace | **project root** (memory is about the project, shared across sessions) | *not verified* where MEMORY.md is read; check `context/` |
| `.agents/` extensions (tools, hooks, skills, agents, MCP config, `nexus.toml`) | workspace | **project root** only. Edits a session makes to `.agents/` inside its worktree take effect after land; the TUI shows "extension changes in this worktree are not live until landed" when the session's diff touches `.agents/` | one manifest per project; hot reload watches the project root only |
| Permission rules and session grants | runtime | **project** (shared by all sessions, as today across tabs) | grants that name paths are stored relative to the root they were granted in; a grant for `src/**` in worktree A applies to `src/**` in worktree B (*decision*, §7 Q1) |
| Hooks | workspace cwd | cwd = **session root**; `NEXUS_WORKSPACE` = session root, new `NEXUS_PROJECT_ROOT` = project root | `docs/extensions.md:120-131` |
| MCP servers | started with workspace cwd | **project root**, shared by all sessions | a filesystem-rooted MCP server sees the main checkout, not the worktree; shown as a warning on worktree sessions using such a server (§7 Q2) |
| Code mode (`code` tool) | workspace | session root | same as tools |
| Subagents | parent's workspace | parent's **session root** (or their own worktree, file 05) | |
| State dir `~/.nexus/projects/<key>/` | per workspace | per **project** | shell cwd files stay keyed by session |
| Session list grouping | by workspace | by project, then root (main first, then worktrees by branch) | file 08 |

## 5. External roots (user-made worktrees)

A user can already create worktrees themselves (`git worktree add ../feat-x`). When Nexus is
launched inside one, or the user picks "New session in…" an existing linked worktree:

- the session's root is that path, `kind = "external"`;
- Nexus **never commits, merges, removes or lands** an external root; it only reads Git there
  (status, diff, log) and takes **hidden checkpoint refs** per turn (file 04), exactly like a
  main-checkout session;
- it appears in the sidebar under its branch, marked "your worktree".

## 6. Algorithms

**Resolving a session's root at turn start** (in the `ProjectHost`, before building the turn's
`ToolContext`s):

1. Look up `SessionRoot` in the index; default `main`.
2. For `worktree`: check the service record is `active` and the path exists and is the expected
   worktree (`git rev-parse --show-toplevel` matches, `.git` file points at the project's common
   dir). If the directory is missing but the branch exists, **recreate** it (file 07 §2). If the
   branch is gone, fail the turn with a labelled error and offer "Detach to main checkout".
3. For `external`: check the path exists and is still a worktree of the project; otherwise fail
   the turn with "worktree <path> no longer exists".
4. Build `PathGuard` for the root (main: today's; worktree/external: `for_worktree`-style with
   the project root read-only).

**Changing a session's root** happens only through file 03 (create, promote) or §5 (external
open), always at a turn boundary, never mid-turn.

## 7. Open questions

1. **Path grants across worktrees**: shared by relative path (recommended; the user approved
   "edit `src/**`" for the project) vs per root.
2. **MCP servers with a filesystem root**: run one instance per worktree on demand (costly) vs
   one per project with a warning (recommended for v1).
3. **`.agents/` edits in a worktree**: live for that session only (needs a per-session manifest)
   vs live after land (recommended, simpler, matches "project config is the main checkout").

## 8. Failure modes

| Failure | Behavior |
| --- | --- |
| Tool tries to write outside the session root | refused by `PathGuard` with the root named |
| Root directory removed by the user mid-session | next turn: recreate (worktree) or labelled error (external) |
| Project root moved/renamed | project `exists=false` (file 01 §9); sessions read-only |
| A hook or extension assumes `NEXUS_WORKSPACE` is the project root | documented change; `NEXUS_PROJECT_ROOT` added |

## 9. Tests

- `tests/test_session_root.py`: event round trip and reducer; index rebuild from logs; missing
  root behaviors.
- `tests/test_session_root_tools.py`: two sessions in one project with different roots; `edit`,
  `shell`, `grep`, `code` act in the right root; writes to the project root refused from a
  worktree session; shell `cd` bounded by the root; `!` composer starts at the root.
- `tests/test_session_root_context.py`: `AGENTS.md` from the root, `.agents/` from the project,
  hooks get both env vars.
- Peers to update: `tests/test_host_facade.py` (Git host commands with `session`),
  `tests/test_layering.py` stays green.
- Exit: full suite green with every session `main` (no behavior change).

## 10. Docs

`docs/sessions.md` (session root), `docs/tools.md` (cwd, PathGuard), `docs/context.md`
(`AGENTS.md` vs `MEMORY.md` source), `docs/extensions.md` (`.agents/` from the project root,
hook env), `docs/events-and-view.md` (`session.root_set`, `view.root`), `docs/host.md` (Git
commands take `session`), `docs/module-map.md` (`session/root.py`).
