# Plan 28: Manager indicator, four states in both clients (2026-11-16)

The wave is drawn with the Manager panel open (the scrim must not dim it). Scratch homes only.

| State | TUI | NSH |
| --- | --- | --- |
| Connected, user speaking (listening colour) | `tui-listening-{dark,light}.png` | `nsh-listening-{dark,light}.png` |
| Connected, Manager speaking | `tui-speaking-{dark,light}.png` | speaking colour is covered by `manager.test.tsx` (the dev pref cannot hold the speaking state with the panel open) |
| Muted (still flat line) | `tui-muted-{dark,light}.png` | covered by `manager.test.tsx` "still marks that never drift" |
| Disconnected (still quiet wave) | `tui-still-{dark,light}.png` | `nsh-still-{dark,light}.png` |

TUI frames come from `NEXUS_TUI_MANAGER_PIN=<state>:0.7 NEXUS_TUI_MANAGER_LIVE=1 --dump-frame --keys "ctrl+p Manager enter"`
(the Manager enabled and its privacy notice accepted in the scratch home). NSH frames come from the `nexus.manager.dev`
storage pref (`live`) through the headless web harness. Not captured in NSH: speaking and muted, which the pref cannot pin.

## Indicator box (NSH)

`onLayout` origins were parent-relative in the headless web run (`{x: 8, y: 6}` for a box that sits at x=1136 in a 1280
window), so the scrim hole was cut at the wrong place whenever it was used. It now rebuilds the box in window space from
sizes (`windowSpaceBox`, the header's right-hand group width), and falls back to a full scrim when the box is not in the
header band. Tests: `manager-cards.test.tsx` (hole present; `windowSpaceBox` cases for web, window-space, unmeasured, out of band).
