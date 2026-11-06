# Provider usage visual comparison

Captured 2026-11-06 against one scratch scripted Core using `NEXUS_USAGE_FIXTURE=1` and the shared `interface/scenarios/provider-usage.json`. Both GET and POST `/api/usage` returned all eight fixture providers; the five-hour and weekly reset offsets were 10,800 and 362,880 seconds. No provider calls were made.

- `tui-170x50` and `tui-120x36`: `--dump-frame`, plus scrolled frames (text, SVG and PNG).
- `nsh-1440x900` and `nsh-390x844`: web at desktop and phone widths, plus scrolled PNGs.

Both desktop clients show Window, Progress, Left, Reserve, Expires in that order; the glyphs, left values and signed reserves match (for example 24% used gives `██▃░░░░░░░`, 76% left and +16% reserve). Known expiries use primary text; headers, unknowns and notes are muted. Full gauges are red at 100% used; other shown fills are green; positive reserves are green and negative ones amber. The Codex limit-reached note, xAI product-credit note and Copilot window detail are absent. Later providers, the error and the snapshot age are reachable by scrolling in both clients. The narrow web capture uses the planned two-line phone layout. Snapshot ages can differ by capture time.

The comparison exposed NSH's muted known-expiry text and TUI truncation of lower provider rows; both were fixed before these captures. The TUI screenshots use the terminal SVG renderer and NSH screenshots use the web renderer, so font rasterization and vertical spacing differ by medium.
