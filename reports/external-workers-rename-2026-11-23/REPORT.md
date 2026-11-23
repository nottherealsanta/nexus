# External workers rename: paired Settings captures

2026-11-23. Same seeded Core (`nexus-tui/tools/webterm/seed.sh`, scratch `NEXUS10_HOME`) for both clients.

- `workers-settings.txt`, `.svg`, `.png`: TUI, 120x40, dark, `s ctrl+, down down down down` (skip the
  first-run tour, then Settings › External workers).
- `nsh-workers-settings.png`: NSH web, 1440x900, dark, same Core through `nexus-nsh/scripts/serve.mjs`, then
  `Meta+,` and the `External workers` section.

Both show the section `External workers`, `Claude Code subagents` with `Agent SDK` / `PTY`, and
`Antigravity subagents` with `off` / `ACP`, plus the same notes and the canonical `[workers]` keys. No
client-specific difference. The legacy `CLI · Antigravity` label is covered by the shared
`interface/scenarios/zmx-denial.json`, not by these Settings captures.
