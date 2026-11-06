# Native inspector clients (PLAN 14.1) - 2026-11-06

Both clients render `interface/scenarios/inspector.json` (Inspector API v2 responses) through
their real renderers; no provider calls.

- `nsh-timing.png`, `tui-timing.png`: Timing, one shared axis, grouped lanes, ruler, first-byte marker, expanded model lane.
- `nsh-conversation.png`, `tui-conversation.png`: Conversation with an expanded, clipped row (`e` loads the full record).
- `nsh-questions.png`, `tui-questions.png`: Questions (NSH inspecting the child session `Scout`; the TUI shot is the root session).
- `nsh-narrow.png` (390x844), `tui-narrow.png` (80x40): narrow layouts.

Regenerate: NSH `nsh test --target web --browser <chromium> inspector.test.tsx` (writes `nsh-*.png`);
TUI `NEXUS_INSPECTOR_SHOTS=$PWD/reports/section14/inspector cargo test --test inspector` writes SVGs, converted with headless Chromium
`--screenshot` (`--window-size=1344,900`; 672,720 for narrow).
