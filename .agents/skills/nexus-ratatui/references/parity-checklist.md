# Native feature audit

Use this to audit a feature against the documented host and surface contracts. Mark an item done only with evidence
(test or screenshot) and record it in `docs/ratatui-parity.md`.

## Per-feature audit

- [ ] Same information, same labels, same order, same empty and error wording.
- [ ] Same shortcut and slash command (and alias); both appear in `/hotkeys` / `/help`
      (`ui_support/shortcuts.py`, `ui/cli/commands.py`).
- [ ] Same behaviour for stale/async results, reconnects, session switch.
- [ ] Keyboard path and mouse path both work; small terminals keep every choice
      reachable.
- [ ] Narrow (about 60 cols), medium (110-169), wide (170+) checked in dark and light.
- [ ] Clipping is announced; nothing the agent sees is hidden from the user.
- [ ] Existing web behavior updated when needed; new web features only when requested (`docs/web.md`).

## Areas and current status (update as you go)

Verified at some level: slash commands, shortcuts and leader keys, transcript
cards/rows, context header, details sidebar, composer chrome, permission/question
panel, pickers, settings/provider/worktree/attachment journeys through the real host.

Open: spinner animation, inline diff with line numbers, details file expansion and MCP
refresh control, populated-context-header screenshot, voice/worktree/settings
screens visually, real audio hardware.

## Verification

Run native unit and workflow tests, layering/docs checks and relevant PTY checks.
Inspect screenshots for visual changes. Report remaining hardware, terminal and
wheel-matrix limitations honestly. `auto` is an alias for Ratatui; missing binaries
report install/build guidance. No merge, publication or version bump unless asked.
