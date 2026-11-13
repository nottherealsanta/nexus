# Layout: tabs, groups and splits

Plan: [45](../plans/45-tabs-and-splits.md). Status: the manifest, this page and the TUI (2026-11-13) are in; NSH lands
alongside (see each client's section for its differences). terminals.md keeps its migration note until both clients
have been verified together.

Desktop and web only: the TUI, NSH on desktop and web, and the iPad (it runs the desktop client). The phone client
keeps its three tabs and ignores all of this. The model sees none of it: tabs, splits and open files are UI state, not
context, so the timeline rule is unchanged.

## The model

The centre, between the two sidebars, is one **layout tree**. A *split* node has a direction (row or column), children
and ratios. A leaf is a *group*: an id, ordered tabs and an active tab. Exactly one group is focused. The tree
replaces the header session strip, the file/diff modal and the terminal dock. Splits nest freely.

A **tab** has a kind and a resource id; the pair is its identity, so the same file or session can be open in two
groups and shares one buffer or one live state.

| Kind | Shows |
| --- | --- |
| `session` | The timeline and its composer |
| `file` | The editor, with today's Markdown / Mermaid preview toggle |
| `diff` | A Git change |
| `terminal` | A user terminal ([terminals.md](terminals.md)) |
| `browser` | The browser resource, when it ships |

The inspector stays its own web page.

## Operations

- **Open** goes to the focused group; an existing tab there is activated. **Open to the side** (Ctrl/Cmd+click, or the
  action) opens in the group to the right, creating it if needed.
- **Split right / down** shows the active file, diff or session again in a new group beside or below (same buffer). A
  terminal tab creates a **new terminal** in the new group, because one PTY's control and size cannot serve two views.
- **Join right / down** moves the focused group's tabs into its neighbour that way (appended; the moved active tab
  stays active) and closes the split. Unavailable, and absent from the menu, when there is no neighbour.
- **Move tab to group** left, right, up or down moves the active tab; the target is focused and an emptied group is removed.
- **Drag:** inside a strip reorders; onto another strip moves (Alt/Option copies); onto a group body shows five drop
  zones (left, right, top, bottom split; centre moves into the group). Dividers resize. Esc cancels. The TUI uses the
  mouse in Ghostty as well as the keyboard actions.
- **Close** closes the active tab. The last tab of a group removes the group and its space goes to the sibling.
- **Limits:** a split that would leave a pane below the minimum (TUI about 20 × 5 cells) is refused with a toast saying why.

## Terminals and unsaved files

Terminal tabs keep the [terminals](terminals.md) contract (control, read-only banner, detach versus terminate). There
is no hiding: closing a terminal tab terminates it, asking first while it runs. A file tab with unsaved changes shows a
dot; closing it, or a group holding it, asks Save, Discard or Cancel. One buffer serves every tab of the file. There are
no preview (italic) tabs.

## Restore and narrow widths

The layout is **client-local** (TUI prefs, NSH `Storage`), keyed by project and worktree, and is not shared through
Core. On start a client rebuilds the saved layout. Tabs whose resource is gone (closed session, deleted file,
terminated terminal) are dropped quietly and empty groups collapse. A fresh worktree gets one group with its sessions.

Below the width that fits the layout's minimums, the client shows only the focused group, with a marker in its strip
that others are hidden. The saved layout is kept, not rewritten.

## Controls

Defined once in `interface/manifest.toml` (ids `layout.*`), shown in the palette and the group menu (the glyph at the
strip's right end). Leader keys follow Ctrl+X.

| Action | Id | Leader | Menu |
| --- | --- | --- | --- |
| Split right | `layout.split_right` | `\` | yes |
| Split down | `layout.split_down` | `-` | yes |
| Join right | `layout.join_right` | `shift+right` | when possible |
| Join down | `layout.join_down` | `shift+down` | when possible |
| Focus group left / right / up / down | `layout.focus_left` ... | `left` `right` `up` `down` | no |
| Move tab to group left / right / up / down | `layout.move_left` ... | `alt+left` ... | no |
| Close tab | `layout.tab_close` | `w` | yes |
| Open to the side | `layout.open_to_side` | none (Ctrl/Cmd+click) | row menus |
| Group menu | `layout.group_menu` | `.` | is the menu |

Alt+X (`/close`), Alt+[ and Alt+] keep their ids (`session.tab_close`, `session.tab_prev`, `session.tab_next`) and act
on the focused group's tabs. Ctrl+\` is `terminal.open`: it focuses the most recent terminal tab or opens one in a
split below the focused group.

## NSH (desktop and web)

Code: `nexus-nsh/src/layout.ts` (the pure tree and every operation, tested in `layout.test.ts`), `src/layout-state.ts`
(signals, persistence, reconciliation, actions) and `src/ui/groups.tsx` (strips, bodies, dividers, drop zones).

- **Minimum pane:** 200 x 120 px (the TUI's 20 x 5 cells at NSH's 9.6 x 24 px text cell, rounded up). A split or a
  drop that would leave either half smaller is refused with the `layout.too_small` toast (`{cols} x {rows}` filled with
  20 x 5), a divider stops at the minimums of everything it separates, and below the width or height that fits every
  group at the minimum the client draws only the focused group, with the `layout.hidden_groups.*` marker in its strip.
- **Persistence:** `Storage` key `nexus.layout.<project id>.<worktree path or "main">`, a JSON tree
  (`{ v: 1, root, focus, seq }`). Session tabs always mirror the project's open sessions (a session opened or closed
  anywhere gains or loses its tab); file, diff and terminal tabs belong to the layout. Switching worktree swaps the
  layout; a file tab with unsaved text is carried over rather than lost. Restore drops a session that is gone or
  archived, a terminal that is closed and a file that no longer reads, without a message.
- **Several sessions side by side:** the viewed session has the composer, so only one group owns it (the focused
  group if it shows a session tab, else the group showing the viewed session). A session tab active in any other
  drawn group shows that session's own live timeline, read-only and following the latest message; focusing the group
  (a press in it, or the leader's arrows) makes it the viewed session. NSH: `PeerTimeline` in `src/ui/timeline.tsx`,
  rows built inside `Model.withView` (the TUI's `swap_in`), views kept by `Model.pin` and loaded by `Sync.watch`
  (again after each resync), driven by `backgroundSessions()` in `src/layout-state.ts`.
- **Terminals:** one controller per terminal (`src/terminal-panel.ts`); a terminal that is the active tab of a drawn
  group is attached and live, so two visible terminals both stream. Terminal screens are one flat layer keyed by
  terminal id and positioned from the layout rectangles, so a terminal tab dragged or moved to another group keeps its
  mounted WebView (no remount, no reattach, no replay).
- **Drag:** a tab drags after 6 px of travel. Over a strip it reorders or moves (insertion marker); over a body it
  shows the five zones (an outlined rectangle over the half or the whole body). Alt/Option copies (never a terminal),
  Esc cancels with the `layout.drop.cancelled` toast. The pointer's modifiers are read from the last mouse move over the
  area, because a press carries none.
- **Esc** no longer closes a file: a file is a tab. Close it with the tab's close button, Alt+X or Ctrl+X W.

## TUI

Code: `nexus-tui/src/groups.rs` (the pure tree, every operation and the tests; no drawing), `src/app/tabs.rs` (the
glue: restore, live state, commands, closing, drag), `src/render/groups_ui.rs` (strips, bodies, dividers, drop zones)
and `prefs.rs` (`layouts`).

- **Cells:** a split puts a one-cell gutter between its children (the divider a drag grabs); a group is its rect, row 0
  its strip. The minimum pane is 20 x 5 cells, strip included. Below the area that fits every group at the minimum
  the client draws only the focused group with the `layout.hidden_groups.*` marker at its strip's right end; the saved
  layout is not rewritten.
- **Persistence:** `tui.json` key `layouts`, one tree per `project + worktree`. The viewed session's worktree decides:
  viewing a session in another worktree swaps layouts as a project switch does (the old one is saved, the other
  restored or fresh); a worktree listing arriving late only names the layout already shown. Every open session has a
  tab in whichever layout is current. Restore drops a session that is gone or archived,
  a file that is not on disk and (once the terminal list is read) a terminal that is gone, without a message; an
  unreadable layout is skipped, not the file.
- **Live content:** the focused group's active tab has the keys (the viewed session with its timeline and composer,
  the open file or diff, the attached terminal). Terminals: every terminal tab that is the active tab of a drawn
  group streams into its own screen (one attachment and one stream task per terminal, `Parked` in
  `terminal_panel.rs`); the focused one is the attached terminal and alone takes control and input, the others are
  read-only, and the PTY resizes only for the controller. Focusing a group takes control on its existing attachment;
  a terminal whose tab is closed, covered, or in a group hidden by the narrow collapse is detached. Files: another
  group shows the last text seen (the shared buffer when it has unsaved changes). Sessions: every drawn session tab
  shows its own timeline, read-only and following the latest message (no composer, no hits); only the focused
  group's session has the composer, selection and input. The reducer keeps a view per session (`Model.cached`, with
  `Model.pinned` views that are never evicted); a drawn unfocused session is pinned and its history is watched
  (`SyncCommand::Watch`: loaded when it first shows and again after each resync), and events reach pinned views as they
  reach the selected one. The timeline of such a tab is built by swapping its view in as the selected one for the build
  (`Model::swap_in` / `swap_out`, `App::background_timeline`, cached by session and width), so `timeline::build` and
  its call sites are unchanged. A tab that is closed, covered, or hidden by the narrow collapse is unpinned and
  dropped from the watch (the same visibility rule as terminals). Not carried to those views: wire/tool scratch
  (live tool output) and the diagnostic events, which only the selected view records, so a background timeline
  shows stored messages, summaries and the streaming text but not in-flight tool detail.
- **Editor:** one buffer per file; a dirty buffer is kept aside when its tab is not in front and given back when
  the file is read again (`stale` when the file moved on). Closing a dirty file tab asks Save, Discard or Cancel
  (Save closes it when the write lands; the group prompt is not needed because only one tab closes at a time).
  Esc ends a find but does not close a file; `Ctrl+X` is the editor's cut while it has the keys, so the leader
  commands are reached from the palette, the group menu or `alt+x`, `alt+[`, `alt+]` there.
- **Pointer:** a press on a tab starts a drag (a release without movement is a click, a click on the viewed tab
  renames the session); over a strip it shows an insertion mark, over a body the zone (reverse video over the half or
  the whole body); Alt copies; Esc cancels (`layout.drop.cancelled` toast). A divider drags within the minimums.
  Ctrl+click on a session, file or changed-file row opens it in the group to the right, as does **Open to the side**
  in the row menu.
- **Terminals:** the PTY resizes with the pane, controller only; see Live content above.
