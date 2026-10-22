# 08. Host protocol and native TUI

Phases: protocol pieces land with the phase that needs them (column "Phase"). TUI work targets
the native Ratatui client only (`rust/tui/`, `nexus/ui/ratatui/`). The web client gets nothing
new (to be deprecated; it must keep working against protocol v4 with one project per tab, file
01 §2). The GPUI desktop client is paused.

All new commands go in `nexus/host/protocol.py`, are handled in `nexus/host/facade.py` (or the
daemon for daemon-scoped ones), return bounded, redacted results, and are mirrored in the Rust
wire structs with parity tests (`tests/test_desktop_wire.py`-style, for the TUI bridge
`nexus/ui/ratatui/wire.py`).

## 1. Host commands

### 1.1 Projects (F1)

| Command | Fields | Result |
| --- | --- | --- |
| `Hello` (v4) | `version=4, client_id, client, focus: str, env: dict[str,str] | None` | `Welcome{…, focus: ProjectRef}` |
| `ProjectList` | `limit: int = 100` (1–500) | `ProjectListResult{projects: list[ProjectRow], truncated: bool}` |
| `ProjectOpen` | `path: str` | `ProjectOpenResult{project: ProjectRef, created: bool}`; refuses missing dirs |
| `ProjectFocus` | `project: str` (key) | `ProjectFocusResult{project: ProjectRef}` |
| `ProjectUnload` | `project: str, force: bool = False` | `{unloaded: bool, reason: str}` |

`ProjectRef{key, root_display, name, kind: "git"|"plain"}`;
`ProjectRow{project, loaded, exists, sessions, running, queued, worktrees, behind,
last_activity}` (file 01 §5). `ProjectSessionsList` keeps working; its `ProjectSession` rows gain
`root: RootRef` (below) and lose the need for `ProjectSessionOpen.socket_path`.

### 1.2 Session roots and worktrees (F2, F3)

| Command | Fields | Result |
| --- | --- | --- |
| `SessionStart` (extended) | `+ worktree: bool | None = None` (None = setting), `base: str = ""`, `root: str = ""` (external worktree path) | as today, `+ root: RootRef`, `+ draft_worktree: DraftWorktree` |
| `SessionWorktreeSet` | `session, enabled: bool, base: str = ""` | `DraftWorktree`; refused after the first turn (`already_started`) |
| `SessionWorktreeStatus` | `session` | `WorktreeStatus` |
| `SessionDetach` | `session, confirm_token: str = ""` | preview then confirm; file 03 §4.3 |
| `SessionPromote` (F7) | `session, continuation: str = "", confirm_token: str = ""` | preview lists carried/left files; confirm creates the worktree |
| `GitStatus`, `GitDiff`, `FilePreview`, `DirectoryList` | `+ session: str = ""` | resolve against the session root (file 02 §4) |
| `GitBranches` (new) | `project: str = "", limit: int = 200, query: str = ""` | local branch names, current branch, target tips |

```
RootRef{kind: "main"|"worktree"|"external", display, branch, base_ref}
DraftWorktree{enabled, available: bool, unavailable_reason, base_ref, base_sha,
              dirty_main: {modified, untracked, clipped}}
WorktreeStatus{root: RootRef, head, base_sha, target_tip, ahead, behind,
               uncommitted: {modified, added, deleted, untracked, conflicted},
               merge_in_progress: bool, last_commits: list[CommitRow] (≤ 5),
               base_moved_unacked: int, queue: QueueEntryView | None, setup: SetupView | None}
CommitRow{sha, seq, by, agent_id, subject, files, insertions, deletions}
```

### 1.3 Per-turn diffs and checkpoints (F3, F4)

| Command | Fields | Result |
| --- | --- | --- |
| `SessionTurnDiff` | `session, seq: int, path: str = "", max_bytes: int = 262144` | `TurnDiff{source: "commits"|"checkpoint", refs: [from, to], files: list[FileStat] (≤ 500), foreign: int, head_moved: bool, patch: str (bounded, only when path given or total small), clipped: bool}` |
| `SessionTurnRevert` (F4, worktree only) | `session, seq, include_later: bool = False, confirm_token: str = ""` | preview then confirm (file 04 §6) |

Reuses `host_support/git_diff.py` limits (`MAX_PATCH_BYTES` 256 KiB) and its ref validation.

### 1.4 Queue and landing (F6, F7)

| Command | Fields | Result |
| --- | --- | --- |
| `SessionSync` | `session` | enqueues a `merger` sync turn (`origin="sync"`); returns the queued input id |
| `SessionSyncAbort` | `session, confirm_token: str = ""` | aborts an in-progress merge |
| `SessionLandPreview` | `session, mode: str = "" (default from config), message: str = ""` | `LandPreview{state, needs_sync, base_moved: BaseMovedView, checks: list[CheckView], review_id, digest, review_pages, files, insertions, deletions, commits (≤ 200), message, target, target_checked_out, main_clean, main_dirty_paths (≤ 20), refusal: str, token, token_expires}` |
| `SessionLandReview` | `session, review_id, page: int, limit: int (1–8)` | paged review (existing `WorktreeReview` paging) |
| `SessionLandConfirm` | `session, token, message: str, checks_override: bool = False, typed_name: str = ""` | `LandResult{landed: bool, new_tip, landed_ref, refusal, detail}` or, in PR mode, `{pr_url, number}` |
| `LandQueueList` | `project: str = "", target: str = ""` | `list[LandQueueView{target, holder, entries: list[QueueEntryView]}]` |
| `WorktreeList` (extended) | `+ kind: "all"|"session"|"subagent"|"legacy" = "all"` | rows gain `kind`, `session`, `branch`, `state`, `dirty`, `disk` |
| `WorktreeCleanup` (F7) | `project, dry_run: bool = True, confirm_token = ""` | cleanup pass preview and apply (file 07 §2.3) |

Tokens follow the existing pattern: HMAC, 120 s, single use, bound to every input that the
preview showed (`nexus/host/facade.py:418-650`).

### 1.5 Events added to the stream

All file 02–06 events are ordinary session events (already streamed). Daemon-level: `project.loaded`,
`project.unloaded`, `project.activity{key, running, queued, behind}` (throttled to 1/s per
project) so the TUI can badge projects that are not focused.

## 2. Slash commands (`nexus/ui/cli/commands.py`)

| Command | Does |
| --- | --- |
| `/project [name|path]` | open the project picker, or switch directly |
| `/worktree` | toggle worktree for a draft session; on a started session, show status |
| `/sync` | `SessionSync` |
| `/land [squash|merge|ff]` | open the Land flow |
| `/queue` | the queue view for this session's target |
| `/diff turn [seq]` | per-turn diff (default: last turn) |
| `/worktrees` | existing command, extended with kinds and cleanup |

## 3. Native TUI

### 3.1 Starting a session: the setup strip

There is no separate new-session dialog today (a new tab is a draft "New Session",
`nexus/ui/ratatui/actions.py:367`). Add a **setup strip** above the composer of a draft session,
which is the "session modal" the toggle must live in (D2):

```
╭ New session · nexus ─────────────────────────────────────────────────────────╮
│ Worktree  [● on]   Base  main ▾   ⚠ 12 uncommitted changes in main not included │
│ Agent  build ▾                                                  Ctrl+T toggles │
╰──────────────────────────────────────────────────────────────────────────────╯
```

- Visible only until the first prompt is sent. `Ctrl+T` (*free key not verified*) toggles,
  Enter on `Base` opens a branch picker (`GitBranches`).
- In a non-git project the toggle renders disabled: `Worktree  [– n/a]  needs a Git repository`.
- After the first send the strip collapses into the context header's first line:
  `⎇ nexus/session/brisk-teal-otter-3fa1c2 · from main@a1b2c3d`.

### 3.2 Top bar and sidebar

- **Top bar** (`rust/tui/src/render/chrome.rs:322-336` shows `path branch worktree` today): for
  a worktree session show `project · ⎇ <session branch> · ↑3 ↓2` (ahead/behind vs target) and a
  `behind` pill in amber when `base_moved_unacked > 0`, `conflicts` in red.
- **Projects tab** (`rust/tui/src/sidebar.rs`, `TABS = ["Projects", "Sessions"]`): one row per
  project (`ProjectList`), with running/queued counts and a dot when a non-focused project has
  activity; Enter focuses (`ProjectFocus`). Under the focused project, its roots: `main`, then
  each worktree branch with its session(s) and state pill (`ready`, `behind`, `conflicts`,
  `landing`, `landed`). External worktrees marked "your worktree".
- **Sessions tab**: grouped by project → root; a filter `worktree: all|main|worktrees`.
- **Quick switcher** (`Ctrl+P`, *key not verified*): fuzzy over `project / branch / session
  name`, so switching between projects and worktrees is one keystroke away (user requirement).

### 3.3 Timeline rows

Labelled rows (never raw JSON; built in `nexus/ui_support/worktree_rows.py`, new):

- `worktree.created`: "Worktree created · nexus/session/… from main@a1b2c3d · 12 uncommitted
  changes in main not included".
- `worktree.setup_ran`: like a shell job row: command, exit, duration, output tail (expandable).
- `worktree.committed`: under each turn: "Committed a1b2c3d · nexus: add retry · 3 files +40 −7
  · by root" (expandable file list); agent/subagent badges.
- `checkpoint.captured`: "Checkpoint · 3 files +40 −7 (2 other files changed in the checkout)".
- `worktree.subagent_merged` / `_conflict`: in the subagent row footer.
- `worktree.base_moved`: an amber notice row: "main moved · 3 commits landed by brisk-teal-otter
  · 12 files, 2 overlap with yours · [Sync]".
- `worktree.synced` / `sync_conflict`, `land_*`, `landed`, `pr_opened`: status rows with the
  relevant counts and links.

Each turn header gets a `diff` affordance (`d` on a focused turn) opening §3.5.

### 3.4 Land flow

`/land` or the session action `Land…`:

1. Calls `SessionLandPreview`. If `needs_sync`: a dialog "This session is 3 commits behind main
   (2 files overlap). Sync first?" [Sync now] [Cancel].
2. Otherwise a full-screen review page (reuse the paged review workflow in
   `nexus/ui/ratatui/workflows.py:686-803`): header with target, mode, commits, files, checks;
   tabs **Files** (paged diff), **Commits** (per-turn list with trailers), **Checks** (output),
   **Message** (editable, multi-line editor reused from the composer).
3. Main checkout state line: "main is checked out in /…/nexus and is clean · will fast-forward"
   or the refusal text with the first 20 dirty paths.
4. Confirm button disabled until every review page was opened (the existing acknowledge rule)
   or, over limits, the session name typed.
5. Result toast and timeline row; other sessions' `behind` pills update live.

### 3.5 Per-turn diff view

A page reached from a turn's `diff` affordance: file list with stats (own files first, then
"also changed in the checkout" collapsed), select a file → bounded patch with syntax colouring
(reuse the existing diff renderer used for `/diff`), clipping announced. For worktree sessions
an `Undo this turn` action (§1.3 `SessionTurnRevert`).

### 3.6 Queue view

`/queue`: per target, holder, ordered entries with state pills, behind counts, overlap, checks;
actions per row: Open session, Sync, Land.

### 3.7 `/worktrees`

Existing list (`nexus/ui/ratatui/workflows.py:686`) gains kind grouping (Session / Subagent /
Legacy), dirty/disk columns, and Cleanup… (dry run, then confirm).

## 4. CLI (non-chat)

| Command | Does |
| --- | --- |
| `nexus chat --worktree/--no-worktree [--base B]` | override the setting for the first session |
| `nexus run --worktree [--base B] "prompt"` | one-shot in a new worktree session; prints the branch |
| `nexus land <session> [--mode …] [--yes-i-reviewed]` | non-interactive land requires the typed confirmation phrase like `nexus worktrees integrate` (`nexus/cli.py:714-722`) |
| `nexus sync <session>` | enqueue a sync turn |
| `nexus projects [open PATH]` | file 01 §7 |

## 5. Tests

- Protocol: `tests/test_host_protocol_worktree_sessions.py` (struct round trips, bounds,
  refusals, token binding); wire parity for every new struct in the Ratatui bridge.
- Facade: `tests/test_host_facade.py` peers for each command.
- UI support: `tests/test_ui_support_worktree_rows.py` (labels, clipping notes).
- Native PTY journeys (`tests/test_ratatui_journeys.py` style, real temp repos): setup strip
  toggle and base picker; non-git disabled toggle; per-turn diff page; base-moved pill and Sync;
  Land review → confirm; project switch from the Projects tab and the quick switcher.
- `docs/ratatui-parity.md` entries for each new screen, marked verified only after the PTY
  check.

## 6. Docs

`docs/host.md` (all commands), `docs/cli.md` (slash and CLI), `docs/surfaces.md` (worktree
state shown the same way wherever shown; web excluded and stated), `docs/ratatui-parity.md`,
`docs/module-map.md` (`ui_support/worktree_rows.py`, `ui_support/worktree_text.py`).

## 7. Open questions

1. Key bindings for the strip toggle and the quick switcher: check `rust/tui/src/input.rs` for
   free keys before choosing.
2. Should non-focused projects' sessions keep streaming to the TUI (more traffic) or only send
   `project.activity` summaries (recommended)?
