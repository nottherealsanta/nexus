# 33. Remove Git from the right sidebar

User request (2026-11-11): remove Git from the right sidebar. Both clients in the same change. Desktop/web only
(mobile has no sidebars).

## Today

- The shared manifest defines two right-sidebar sections: `session_info` (order 1) and **`git_status`** ("Git
  status", order 2) (`interface/manifest.toml` ~196–205).
- TUI: drawn in `nexus-tui/src/render.rs` (~2283 "Loading Git status…", ~2502 Staged/Unstaged/Untracked, ~4048 label
  `Git`), fed by `api.rs::git_status` and `app.rs` `git_status` (~659, ~3709–3750).
- NSH: `nexus-nsh/src/ui/right.tsx` (`GitGroup`, `GitRows`, the section at ~243–248), fed by `gitStatus` in `store.ts`.
- The **left** sidebar's Git tab (`sidebar.tab.git`, the full Git page from [09-backlog.md](09-backlog.md) 9.2) is a
  separate surface and **stays**.

## Plan

- [ ] Confirm with the user: only the right sidebar's "Git status" section goes; the left sidebar Git tab, the branch
  in the location strip and the Git diff modal stay. (Default: yes.)
- [ ] Remove the `git_status` section from `[navigation.right_sidebar]` in the manifest; regenerate NSH's manifest;
  `check_manifest.py`.
- [ ] TUI: remove the section's drawing, hit targets and keyboard focus stop; remove the right-sidebar Git status
  fetch and state if nothing else uses them (check the Git tab and strip first). The right sidebar keeps Session info.
- [ ] NSH: remove `GitGroup`/`GitRows` and the section from `right.tsx`; drop the `gitStatus` fetch when the right
  sidebar opens if nothing else uses it.
- [ ] Core: `GET …/git` status route stays if the left Git tab or anything else uses it; otherwise note it as unused,
  do not delete in this change.
- [ ] Tests: update right-sidebar tests/scenarios in both clients (they assert the Git section today); screenshots of
  the right sidebar in both clients.
- [ ] Docs: both READMEs, `interface/design-language.md`, `interface/capability-matrix.md`; ledger row if old Nexus
  had Git status in the right sidebar.
