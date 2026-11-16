# Plan 26: launch splash, cold start (2026-11-16)

Scratch homes only (`/tmp/splash/...`, ports 479xx); Core delayed 8 s by a wrapper (`NEXUS_TUI_CORE_CMD` /
`NEXUS_NSH_CORE_CMD`) so the splash waits past the 5 s slow line.

| File | What it shows |
| --- | --- |
| `tui-cold-start-02.txt` | TUI at t=2 s: ambient dots and the logo, no bar or percentage |
| `tui-cold-start-07.txt` | TUI at t=7 s: the quiet `Starting Nexus Core…` line under the logo |
| `tui-cold-start-12.txt` | TUI at t=12 s: the live shell (empty workspace) |
| `nsh-cold-0.5s.png`, `nsh-cold-6.5s-slow-line.png`, `nsh-live-13s.png` | NSH web at the same moments |
| `nsh-cold-start.gif` | NSH web, the whole cold start (8 fps; the light sweeping across the logo) |

The TUI was run in tmux (text frames, one per second, 110x32); the frame at t=2 differs from t=3, so it animates
while Core starts. Real Ghostty by hand is on the user checklist.

## Sweep cost in NSH (30 Text nodes, 30 Hz), headless Chromium, 6 s windows

| | main-thread task ms per second | script ms per s | frames (6 s) | frame p50 / p99 |
| --- | --- | --- | --- | --- |
| splash animating | 77 | 54 | 865 | 6.9 / 8.5 ms |
| live, idle | 15 | 3 | 865 | 6.9 / 8.5 ms |

About 62 ms per second of extra main-thread time (~6% of one core), only while Core is starting; the frame rate is
unchanged. Not worth optimising.
