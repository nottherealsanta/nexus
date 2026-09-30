# Discovery and context controls

1. Discover global and exact-project MCP files automatically, with project names overriding global names; retain bounded parsing and failure behavior. Preserve skill scope through the host.
2. Persist individual skill/MCP choices in session records. Filter skill indexes, invocation, bundled tools, MCP tools and indexes. Lock choices and agent changes once a turn starts, including completed sessions.
3. Show Project N | Global N in both context headers and open individual controls from Skills/MCP. Explain the cache lock.
4. Verify discovery, replay, execution filtering, host enforcement, TUI interaction, web parity, layering and real-browser TUI journeys.

## Implemented behavior

- Automatic global/project discovery with deterministic project MCP precedence and both supported JSON map spellings.
- `Project N | Global N` counts in TUI and web; counts include discovered entries switched off for the session.
- Individual choices are session-scoped and durable. The first recorded turn locks skills, MCP and root-agent selection, including after reconnect or completion.
- Disabled MCP tools and resources are inaccessible to root, child and grandchild agents. Disabled skills cannot be invoked or contribute scoped tools.

## Validation

- Full offline pytest suite: **4,669 passed, 313 skipped, 3 deselected**.
- Latest targeted runtime, selection, reasoning, layering and UI checks: **181 passed**.
- Focused real-host TUI Playwright checks: both global and project entries, repeated on/off changes, and first-turn locks at **1440px and 900px**.
- Focused web Playwright checks: matching controls through the daemon, repeated changes and first-turn locks at both widths.
- Ruff and `git diff --check`: clean.
- Existing broad TUI browser checks retain model-picker keyboard/rejection failures and intermittent logs polling timing failures. These reproduce on the original checkout; the logs journey passed an isolated worktree rerun. The broad web check stops at its pre-existing font assertion. These unrelated behaviors were not changed.

Screenshots and browser telemetry are under ignored `artifacts/context-controls/`.
