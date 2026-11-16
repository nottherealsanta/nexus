# Plan 45: tabs and splits, paired captures (2026-11-16)

Dark and light for every state. TUI frames come from `--dump-frame` on a scratch Core (and from the TUI test harness
rendered to SVG for the drag and prompt frames); NSH frames from the headless web harness. Scratch homes only.

| # | State | TUI | NSH |
| --- | --- | --- | --- |
| 1 | One group | `tui-1-one-group-*` | `nsh-1-one-group-*` |
| 2 | Split right (`ctrl+x \\`) | `tui-2-split-right-*` | `nsh-2-split-right-*` |
| 3 | Split down (`ctrl+x -`) | `tui-3-split-down-*` | `nsh-3-split-down-*` |
| 4 | 2 x 2 grid | `tui-4-grid-*` (four session panes, no terminal tab) | `nsh-4-grid-with-terminal-*` (a terminal tab beside two files) |
| 5 | Drag in progress, drop zone | `tui-5-drag-zones-*` (reverse-video zone on the right edge) | not captured: the web runner refuses a held drag (`dragAndHold` is headless only) |
| 6 | Join (`ctrl+x shift+right`) | `tui-6-join-*` | `nsh-6-join-*` |
| 7 | Narrow collapse (`1 group hidden`) | `tui-7-narrow-collapse-*` (40 columns) | `nsh-7-narrow-collapse-*` (390 px) |
| 8 | Unsaved-close prompt | `tui-8-unsaved-prompt-*` | `nsh-8-unsaved-prompt-*` |

Known gaps: a terminal tab in the TUI grid (the scratch Core has no PTY seeded) and an NSH drag-in-progress frame. Both
behaviours are covered by tests (`tests/layout_tabs.rs`, `layout-input.test.tsx`); a hand drag in real Ghostty and real Chrome
is on the user checklist.
