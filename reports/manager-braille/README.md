# Manager braille indicator and panel focus (11-05)

Captured from a scratch scripted Manager Core (`nexus-core/scripts/scripted_manager_core.py`) on an isolated
`$NEXUS10_HOME`. No provider calls and no real home.

| File | What it shows |
| --- | --- |
| `manager-wave-idle.png` | TUI, 170×50 dark: the header field in idle (slowest, dimmest), call open and dropdown closed |
| `manager-wave-speaking.png` | TUI, same state with the speaking signal: the same field, brighter |
| `manager-stop.png` | TUI, live voice on with the pointer on the field: the in-place **Stop** label, braille on either side |
| `tui-stop.txt` | The text frame behind `manager-stop.png` (the label is readable without a picture) |
| `manager-idle.png` | TUI: the open dropdown dimming the rest of the screen |
| `manager-wave-light.png` | TUI, 120×36 light: the gray field in the light theme |
| `nsh-manager-wave.png` | NSH web, 1440×900 dark: the same ten-cell field in the header |
| `nsh-manager-panel.png` | NSH web: the open dropdown over the full-viewport dimming backdrop |

The packaged NSH web host does not read the `nexus.manager.dev` live override (pre-existing), so the NSH **Stop**
state is covered by `nexus-nsh/manager-cards.test.tsx` (hover, then click sends `/api/manager/live/stop`) rather
than a screenshot.
