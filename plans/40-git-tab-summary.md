# 40. Git tab: active worktree only, a summary of changes, no branch list

User request (2026-11-11), about the left sidebar's Git tab:

- Remove the branch list.
- Show only the active worktree, and only its changes.
- Show a summary of the changes ("this many changes") rather than every single file and folder.

Both clients in the same change; desktop/web only (the Git tab does not ship on mobile).

Screenshot (TUI, worktree `nexus-plan-35`): `Git`, the worktree path, the branch, empty `Staged` / `Unstaged` /
`Untracked` headings, then `Branches` listing every local branch (~130 rows, the current one marked `●`). The
branch list fills the tab and is the only thing on it.

## Today

- TUI: `nexus-tui/src/render.rs` ~2267–2337 draws the tab: title, `data["worktree"]`, `data["branch"]`, the three
  groups with one row per file (hit targets `Target::SideRow`, cursor from `app.project_git_rows()` in `app.rs`
  ~3901–3990), then `Branches` from `data["branches"]`.
- NSH: `GitTab` in `nexus-nsh/src/ui/sidebar.tsx` ~75–95, same layout; per-file rows have a context menu
  (`git.stage`, `git.unstage`, `git.discard`) and open the diff on press. Fed by `loadGitPage` / `gitPage` in
  `store.ts` for `currentWorktree()`.
- Core: the Git page route returns `worktree`, `branch`, `changes` and `branches`.
- Background: [09-backlog.md](09-backlog.md) 9.2 (the whole Git page); [33](33-remove-git-from-right-sidebar.md)
  removes the right sidebar's Git status, so this tab becomes the only Git view besides the diff modal.

## Plan

- [ ] Confirm with the user (defaults in brackets):
  - What "active worktree" means: the worktree of the open session, else the project's main checkout. [yes]
  - What the tab shows with no changes: one muted line `No changes`. [yes]
  - Summary shape: a header line like `3 files changed · +120 −14`, then counts per group
    (`2 staged · 1 unstaged · 0 untracked`), then the changes grouped **by top-level folder** with a file count each
    (`nexus-tui/  2`, `plans/  1`), not every file. [yes]
  - Can a folder row expand to its files, so stage/unstage/discard and opening a diff still work from the tab? Or is
    the tab read-only and the diff modal the place for per-file actions? [expandable, collapsed by default]
- [ ] Remove `Branches` from both clients. Branch switching stays in the `session.worktrees` picker (the location
  strip's branch button). Core may keep returning `branches`; stop rendering it and note it as unused.
- [ ] Header: keep the branch on one line with the worktree folder name; drop the full path line (the worktree info
  modal from [32](done/32-worktree-indicator.md) has it).
- [ ] Core: add the line counts the summary needs (`git diff --numstat` for staged and unstaged) to the Git page
  response if it does not have them; keep `changes` for expanded folders. Core test with a scratch repo.
- [ ] TUI: rewrite the tab body as summary + folder rows; keyboard cursor and `SideRow` hits over folder rows (and
  file rows when expanded); Enter/click expands a folder, Enter on a file opens the diff as today.
- [ ] NSH: the same in `GitTab`; the row context menu moves to file rows inside an expanded folder.
- [ ] Shared manifest/strings for the labels (`No changes`, the summary format) so both clients read the same text.
- [ ] Tests: TUI render/behaviour tests (no changes, mixed changes, many files in one folder, expand/collapse); NSH
  browser test the same; paired screenshots of the Git tab, including a repo with many branches to show the list is
  gone.
- [ ] Docs: both READMEs, `interface/design-language.md`, `interface/capability-matrix.md`, the Git page notes in
  [09-backlog.md](09-backlog.md) 9.2; ledger row if old Nexus listed branches in its Git page.
