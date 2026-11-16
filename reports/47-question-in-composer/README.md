# Plan 47: the question in the composer slot, TUI and NSH side by side

Captured 2026-11-15 from one scripted Core (`nexus-nsh/e2e/scripted_core.py`, seeded by `e2e/seed.mjs`), session
"Pick the migration database", dark theme. TUI at 170x50 cells (`--dump-frame`), NSH web at 1440x900 (Playwright).

| State | TUI | NSH |
| --- | --- | --- |
| Options have the keys | `tui-question-170x50.png` | `nsh-question-1440x900-options.png` |
| Typed `no`: the line has the caret | `tui-question-170x50-line.png` | `nsh-question-1440x900-line.png` |

Both show one docked panel with one rail, the same hints (`↑↓ move · space select · enter send · esc stop`, then
`esc options · enter send` in the line) and the `Add context…` placeholder. Fixed from this comparison: NSH's rail broke
between the card and the line (`StatusRail` `join`, and no transparent border on the docked surface).

Known difference, older than plan 47: NSH shows "Scroll or Page Up/Down to read questions and choices" at the top of the
card; the TUI does not.
