# Session heading, root back, marquee and gutters (2026-11-11)

Latest visual evidence for the session-page chrome, superseding
`reports/session-title-gutters/` (which predates the blank/root heading, the single metadata row and the full-height subagent back rail).

| Capture | Client / state |
| --- | --- |
| `tui-160x45-root.png` | TUI, root session: one blank heading row under the tabs; three-action sidebar footer pinned at the bottom. |
| `tui-160x45-child.png` | TUI, viewed subagent: full-height four-cell muted back strip with a centred `←`, then the child title and right-aligned agent/model/provider/effort; no second subagent-tab row. |
| `tui-120x36-root.png`, `tui-120x36-child.png` | The same at the medium review size. |
| `tui-120x36-empty.png` | TUI, empty workspace: the sidebar footer stays at the bottom under an empty session list. |
| `nsh-1440x900-root.png` | NSH, root session: blank 28 px heading row under the tabs. |
| `nsh-1440x900-child.png` | NSH, viewed subagent: full-height `textCellPoints() * 4` muted back strip with a centred arrow, then the child title and right-aligned metadata; no second subagent-tab row. |
| `nsh-1024x768-child.png` | NSH at 1024 × 768. |
| `nsh-1440x900-tab-hover.png` | NSH, long tab title mid-marquee: text is clipped to its own title region and the `×` slot stays fixed. |
| `nsh-1440x900-empty.png`, `nsh-1024x768-empty.png` | NSH, empty workspace: the three-action footer stays pinned under the empty list. |

The TUI captures use the scripted Core in the NSH visual harness (`e2e/scripted_core.py`) at
160 × 45 and 120 × 36, reduced motion, dark theme. The NSH captures use the shared mock network
scenario from `child-navigation-parity.test.tsx` at 1440 × 900 and 1024 × 768; the child mock has no
messages, so its timeline body differs from the TUI's populated child. The four-cell full-height rail
and the single heading row (title plus metadata) are the comparison. The rail stops above a terminal dock in both clients;
the TUI draws the dock inside the centre column and NSH draws it below the sidebar/Centre row, so
the surrounding band differs, but neither rail overlaps dock content.

The older `reports/session-title-gutters/` captures remain for history; they show the removed
root-title heading row and predate the latest footer pinning.

Focused checks at capture time: TUI `shell` 172 passed/1 ignored (one unrelated ZMX settings-order failure;
the root-no-rail, four-cell rail, centred arrow, exact composer geometry, dock no-overlap, metadata balance
and tab-marquee clipping tests pass), `timeline` 71/71, and `alerts` 11 passed/1 ignored. NSH
`child-navigation-parity` 7/7, `fork-subagent` 6/6, `timeline-layout` 10/10 and `header-marquee` 4/4 pass.
Parent-run TUI integration covered lib 394 and timeline 71/71 with only unrelated current-tree failures; full
`cargo test` stayed blocked by `tests/scenarios.rs`. At final check, `npm run check` was blocked by unrelated
concurrent `composer.tsx` errors (`TS` missing and `ButtonProps` `onMouseEnter`).
