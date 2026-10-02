# Parity checklist and replacement gates

Use this to audit a feature against Textual. Mark an item done only with evidence
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
- [ ] Web client updated if the behaviour is user-visible (`docs/web.md`).

## Areas and current status (update as you go)

Verified at some level: slash commands, shortcuts and leader keys, transcript
cards/rows, context header, details sidebar, composer chrome, permission/question
panel, pickers, settings/provider/worktree/attachment journeys through the real host.

Open: spinner animation, inline diff with line numbers, details file expansion and MCP
refresh control, populated-context-header screenshot, voice/worktree/settings
screens visually, Textual's agent form fields in native settings, real audio hardware.

## Gates before removing Textual

1. Interaction and visual audits above complete for every area.
2. Full offline suite plus the Textual timing-sensitive group pass.
3. Performance: long history and streaming measured (patch protocol if needed).
4. Distribution: hosted wheel matrix (Linux/macOS x86-64/ARM64, Python 3.13/3.14,
   musl), clean-install launch without Textual (add a CI import check), installer and
   `nexus update` behaviour, and a story for platforms without a native wheel
   (binary-less fallback wheel, or keep Textual as the fallback).
5. `--renderer auto` currently picks native when the binary exists, else Textual;
   only drop Textual from runtime dependencies when gate 4 is verified on hosted runners.
6. Docs, plan and ledger updated; reviewed commit with Conventional Commit subject.
   No merge, publication or version bump unless the user asks.
