# Settings, scroll bar and sidebar selection (2026-11-06)

Captured against scratch homes only (`NEXUS10_HOME` under /private/tmp, fixture Core for NSH web).

| File | What it shows |
| --- | --- |
| `tui-sidebar-selection-120x36-dark.png`, `-light.png` | TUI: the selected project tile and selected session row share one lighter block with a left bar; the selected title is white (dark) and not bold. |
| `nsh-sidebar-selection-1280x800-dark.png` | NSH web: the same block and bar on the project tile and the session row, the same token colours. |
| `tui-settings-120x36-dark.png`, `nsh-settings-1280x800-dark.png` | Settings with the segmented value controls and the Scroll bar row in both clients. |
| `tui-timeline-scrolling-120x36-dark.png` | The TUI timeline after PageUp (the scroll bar shows only while scrolling). NSH's scrolling frames are in `reports/settings-scrollbar/`. |

The shared tokens are `interactive.active` and `interactive.active_bar` (plus `text.highlight`
for the selected title) in `interface/theme.toml`.
