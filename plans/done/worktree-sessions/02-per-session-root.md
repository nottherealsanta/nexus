# 02. Per-session root

Phase: **F2**. Depends on F1 (file 01). No user-visible change on its own: every existing
session's root is its project root.

## Implementation ledger — 2026-10-22

F2 is implemented. Existing sessions without a root event still use the project
checkout. The protocol is now **v5**; no compatibility shim is provided.

- [x] Immutable `SessionRoot`, bounded relative event serialization,
  `session.root_set`, `ConversationView.root`, replay and fork inheritance.
- [x] Runtime-owned lazy root index (512 entries), refreshed against durable
  sequence/creation metadata. Roots anywhere on disk use a relative spelling
  from the project parent, including `..`; the spelling is never authority.
- [x] Before a turn or session-scoped file/Git operation, authenticate canonical
  project identity, Git common directory and the worktree's reverse `gitdir`
  registration. Foreign roots, missing roots and copied `.git` pointers fail
  closed. Owned roots require an active authenticated service record.
- [x] Root changes hold the session lock and refuse active turns, pending work,
  queued input and shell jobs. The durable queue is rechecked under the lock.
- [x] Fresh sessions launched in a user worktree adopt it as `external`.
  Reopening an existing session, including a friendly-name reference, retains
  its root. `ProjectFocus(path)` preserves that path; focus by project key uses
  the canonical checkout. In-root launch subdirectories initialize shell cwd.
- [x] Tool managers, dispatchers, code-mode nested calls and child runners use
  the selected root. Rebased hard guards intersect existing injected/agent
  restrictions; the main checkout and other roots remain outside write scopes.
- [x] Shell cwd/logs remain under the project's state key and explicit state
  home. `cd` is bounded by the root. Composer `!` starts at the root, regardless
  of saved shell cwd, and revalidates it before spawn. A failed auxiliary cwd
  write reports a notice without falsely reporting that root selection failed.
- [x] Root AGENTS instructions freeze for each turn and stay frozen across
  iterations and read-only context inspection. Project SOUL/MEMORY/config,
  extensions, hooks and MCP remain project-owned. Hooks get session cwd,
  `NEXUS_WORKSPACE` and `NEXUS_PROJECT_ROOT` without shared-manager mutation.
- [x] Git status/diff, directory listing, file preview and file search accept a
  session selector. Client, native sidebar, `/diff` and completion forward it;
  stale same-project results are rejected after switching sessions.
- [x] The shared local Claude SDK provider takes a per-request execution root;
  worker cleanup retains process leases through exit/cancellation.
- [x] Matching docs, module map, event/view golden fixtures and regression tests.

Decisions and boundaries:

- Relative configured filesystem rules (`write(src/**)`) match within each
  root after canonical hard-boundary checks. Denies precede grants/allow.
  Exact path grants replay by the relative file within the **same session**,
  using root history; canonical audit records remain unchanged. The baseline
  has no project-wide grant store, so no approvals are imported from other
  sessions. Shell/MCP keys and explicitly absolute globs keep their meaning.
- One project manifest/config/hook manager and one MCP connection set remain
  authoritative. Worktree-local `.agents/` content is not discovered. A labelled
  root notice explains these semantics and the fact that MCP uses the project
  checkout; diff-specific extension badges belong to file 08.
- PathGuard confines path-aware tools and shell `cd`. Arbitrary shell commands
  retain the existing command-permission model; cwd checks are not OS sandboxing.
- Worktree provisioning, promotion and per-turn commits are file 03. Recovery
  that recreates a missing owned checkout is file 07. F2 refuses that turn rather
  than recreating or silently switching roots. External roots are never
  committed, removed or landed by root selection.
- Native clients consume the new selectors. The deprecated browser keeps its
  existing UI; the host contract accepts explicit session selectors for it.

Verification: the final targeted root/runtime/host/provider/shell run passed
**217 tests**. The real native PTY journey verifies a composer command creates
its result in the external checkout, with no file in main. The existing real
Chromium project-session check passes. Permission/tool-focused checks passed
243 tests; client/sidebar/completion-focused checks passed 175 tests. Ruff and
whitespace checks pass. The full run finished with **8,090 passed, 364 skipped,
6 deselected, 16 failed** in 1,194.90 seconds. Twelve native rendering failures
and the placeholder-search and skills-timeout failures match the baseline
recorded in file 01. The other two exposed a stale browse-test helper signature
and an optional injected iteration guard; both were corrected. The complete
reasoning/context rerun passes **44 tests**, browse/root-host passes **42**, and
the final runtime/root tools/permissions run passes **50**. This is not a green full suite; the unrelated baseline failures
remain outside this change.


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

The project Runtime keeps a lazy, bounded `session_id → SessionRoot` index.
It replays the last root event when a session is opened or its durable revision
changes, avoiding a scan of all historical sessions when a project loads.

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
| Permission rules and session grants | runtime | **project configuration**, with grants local to each session log | configured relative rules apply across roots; exact file grants follow a relative path when that session changes root (§7 Q1) |
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

## 7. Resolved decisions

1. Path policy uses relative filesystem rules. Exact file grants are portable
   across one session's roots while preserving canonical audit records; grants
   are not implicitly shared across different sessions. A project grant store
   would be a separate permission feature.
2. MCP stays one instance per project; the root notice states its checkout.
3. Worktree `.agents/` changes are not live until landed; no per-session manifest.

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
