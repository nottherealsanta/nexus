# 39. Sessions list: no worktree header for a one-session worktree

User request (2026-11-11): in the left sidebar's Sessions tab, if a worktree holds only one session, do not show
the worktree group header. Both clients in the same change; desktop/web only (mobile shows no worktree grouping).

Screenshot (TUI): the project shows `Work on Plans 29-32 / ⌥ main`, then a header `⌥ plan-35-timeline-notice-rows`
above its only session `Commit, Push, and Plan 35 … / ⌥ plan-35-timeline-notice…`. The header repeats the branch the
row's second line already shows.

## Today

- TUI: `App` builds the session entries in `nexus-tui/src/app.rs` ~3063–3095. A row gets `tree: Some((label, path))`
  (the group header) when it starts a new root, the project has more than one root (`multiple`) and the worktree is
  linked (`main == false`). `render.rs` ~3613–3621 pushes `SRow::Tree(i)` and draws it at ~3650 (`⌥ label` plus a
  short path when there is room).
- NSH: the session row's place line is `SessionRow` in `nexus-nsh/src/ui/sidebar.tsx` ~383–391 (`⌥ branch`). Find
  NSH's worktree group header (or confirm it has none and the clients already differ) before changing anything.

## Plan

- [ ] Rule: show the worktree header only when the group has **two or more sessions** (root sessions in that
  worktree, in the list as filtered: archived filter and scope apply). Children (`↳`) do not count. A one-session
  linked worktree shows the plain row; its second line keeps `⌥ branch`, so nothing is lost.
- [ ] Confirm with the user: count root sessions only, and recount when the archived filter changes. (Default: yes.)
- [ ] TUI: count sessions per `(project_id, root)` next to `roots` in `app.rs` and add `count > 1` to the `tree`
  condition. Cursor/hit-target indices follow from the row list, so check keyboard navigation and drag reorder still
  land on the right row.
- [ ] NSH: the same rule in the same change (parity skill). If NSH has no header today, bring it in line with the
  TUI first or record the gap.
- [ ] Tests: TUI render test (one-session worktree: no header; two sessions: header), NSH browser test the same;
  paired screenshots of the Sessions tab.
- [ ] Docs: both READMEs and `interface/design-language.md` (sessions list).
