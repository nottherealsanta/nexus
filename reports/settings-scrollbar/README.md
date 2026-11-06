# Settings segmented values and the shared timeline scroll bar (2026-11-06)

Captured from a fake/fixture Core through each client's real renderer; no real
`~/.nexus` was opened.

| File | What it shows |
| --- | --- |
| `tui-settings-segments-120x36-dark.png` | TUI Settings › Appearance with the selected `Timeline detail` row and its `low / medium / high` segments; the hint row reads `enter select · ←→ value · esc back`. |
| `tui-settings-segments-80x24-dark.png` | The same pane at 80×24; the Scroll bar row and its segments still fit. |
| `tui-timeline-scrollbar-120x36-dark.png` (`*.txt`) | The TUI timeline scrolled up. The subdued scroll bar is the short `┃` run in the right gutter (text rows 12–19 of the frame). |
| `nsh-settings-scrollbar-1440x900-dark.png` | NSH Settings with the `Scroll bar` row, its `off / on` segmented control and the shared wording/placement. |
| `nsh-timeline-scrollbar-1440x900-dark.png`, `nsh-timeline-scrollbar-clip.png` | NSH's timeline after a wheel scroll; the clip is the right edge at 4× so the 4 px `interactive.scrollbar` thumb is visible. It hides after the idle timeout. |

The TUI frames were produced with `target/debug/nexus-tui --dump-frame` and the
seeded scratch Cores from `tools/shots.sh`/`webterm/seed.sh`; NSH used
`e2e/up.sh` and a Playwright capture against the scripted host on port 4820.
