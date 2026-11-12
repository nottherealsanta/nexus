# Plan 10: fork subagent visual check (partial)

Status: **NSH captures taken; TUI captures incomplete.** The plan's visual item is not ticked. The TUI child
capture (with the right sidebar) is blocked, see "Open".

## Setup

- Seed: `reports/10-fork/visual_core.py`, run with `NEXUS10_DEV=1`, a scratch `NEXUS10_HOME` under `/tmp` and
  `NEXUS_PORT=49311`. It creates a new parent session each time it runs, so the captured home was seeded once and
  then restarted with a copy that does not create or prompt again.
- NSH: `npm run build` in `nexus-nsh`, then `NEXUS10_HOME=<home> NEXUS_NSH_NO_START=1 node scripts/serve.mjs
  --port 49312 --static dist/Nexus`. Playwright (Chromium) at 1440x900, `colorScheme: dark`.
- TUI: `nexus-tui --dark` run interactively in tmux at 170x50 from the seeded workspace.

## Files

| File | What it shows |
| --- | --- |
| `nsh-parent-collapsed-1440x900-dark.png` | Parent session, groups folded (before the click on `2 forks`) |
| `nsh-parent-forks-expanded-1440x900-dark.png` | Parent, `2 forks` expanded: tree lines and both fork rows |
| `nsh-child-open-no-sidebar-1440x900-dark.png` | Fork child "Pick a file" opened from the parent's `open fork` link, no right sidebar |
| `nsh-child-right-sidebar-1440x900-dark.png` | Same child with the right sidebar open (Session info) |
| `tui-parent-collapsed-170x50.txt` | TUI dump of the parent, groups folded (text only; no PNG yet) |

## Observed differences

- **Right sidebar, Agent row.** NSH shows `Agent  build` for the fork child, the same value as the parent. The
  TUI maps a fork's agent name to `fork` (`nexus-tui/src/api.rs`, around line 281), so the two clients differ in
  principle. The TUI child could not be captured, so the TUI side of this row is not verified.
- **Inherited block.** In NSH the child opens with a folded `› Forked from Fork subagents · 3 messages` row and the
  note "Turns above have no details …". The row has no tree line beside it. The fork rows in the parent do have
  tree lines (`├`/`└`) in NSH.
- **Sidebar titles.** The child appears in the NSH sidebar as a `↳ Fork subagents` row, not `Pick a file`. The TUI
  sidebar also listed the child row as `Fork subagents`. Not checked whether that is intended.
- **Opening a fork.** In NSH a click on a fork's title only expands its inline card; the `open fork ↗` link under
  the expanded card opens the child.
- **Detail level.** In the TUI, `alt+d` expands the fork group as well as every tool (the group shows `▾ 2 forks`).
  NSH's parent view was captured by clicking `2 forks`, which expands only the group.

## Open

1. **TUI child capture.** The child session's timeline is blank in the TUI, in both `--dump-frame --open first`
   and interactive runs. Only the System prompt/context block renders; the Core returns the child's seven messages
   (`/api/snapshot?session=`), and NSH renders the same session. Not diagnosed (no TUI error is logged). This is
   the blocker for the sidebar capture on the TUI side.
2. **TUI key path for dump mode.** `--keys "alt+d"` did not expand the fork group in `--dump-frame`, and the
   `alt+k` + `up`/`down` sequences did not reliably move the timeline selection. Interactively, `alt+d` works and
   `alt+k` restores the last selected block (often the top context block). No working dump key sequence was found;
   no product code was changed.
3. **TUI parent PNG.** Not produced. The interactive expanded parent was reached once but not captured with colour.
4. **Parent notices.** The first seeded home gained about ten `[Nexus notice: tool availability changed]` rows
   after stray key presses toggled a tool in the TUI. Not a seed bug; the seeded home used for the NSH captures has
   none.
