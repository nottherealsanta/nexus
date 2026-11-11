# Nexus plans

The plan used to be a single `PLAN.md` at the repo root. Since 2026-11-11 it is this folder: one file per piece of
work; `PLAN.md` was removed. The plan before 2026-11-06 is kept whole in
[docs/archive/PLAN-2026-11-06.md](../docs/archive/PLAN-2026-11-06.md). **Section numbers quoted by older docs and code
comments ("PLAN.md 8.2", "14.1") refer to that file.** Section numbers below (1–40) are the ones from the
2026-11-06 plan where they existed, so "section 6" still means ZMX. Current behavior is in
[docs/README.md](../docs/README.md); deliberate changes from old Nexus are in the
[porting ledger](../docs/porting-ledger.md).

## Rules every item keeps

- Preserve the visible, ordered context timeline: everything the model receives is shown in order, the UI shows nothing
  the model does not have, wire events are delivered live, deletion and clipping are announced. Exceptions: the Manager's
  own context ([docs/manager.md](../docs/manager.md)) and ZMX children ([zmx-design.md](zmx-design.md) section 4.3).
- **The TUI (Rust) and NSH (web/desktop) are separate, maintained peers** with a similar design and the same functions.
  The TUI is the reference and NSH always mimics every TUI function; a UI change lands in both in the same change
  ([.agents/skills/nexus-ui-parity/SKILL.md](../.agents/skills/nexus-ui-parity/SKILL.md)). A difference needs the user's
  approval and a ledger row. (Decided 2026-11-06: the plan to retire `nexus-tui` for the NSH terminal backend is dropped.)
- **Mobile is a cut-down client** ([05](05-mobile-and-design.md)). Desktop surfaces never ship there.
- Task tools act on the workspace; changes to Nexus state belong to the control plane.
- State is in `~/.nexus` after `nexus migrate`. Never run it against the real home from an agent session; tests use a
  scratch `$NEXUS10_HOME`. Real-provider calls and anything that spends money need the user's approval first.
- CI is deferred (user, 2026-11-04). Do not work on it unless asked.

When an item lands: tick it in its file, add a one-line record to [landed.md](landed.md), and update the docs it
touches. When a whole file is done, move it to [done/](done/). Keep "not verified" honest. Decisions go in
[decisions.md](decisions.md).

## Open work, in suggested order

Re-audited 2026-11-11. New requests from the user on 2026-11-11 are marked **new**.

| # | File | State | Next step |
| --- | --- | --- | --- |
| 41 | [Terminal is the user's; the bash tool is separate](41-terminal-user-only.md) | **landed 2026-11-11**; dock is user-only, bash is pipe-only, `N shell` opens the jobs list in both clients | Inspector `job` lane (follow-up); paired TUI/NSH screenshots not verified |
| 40 | [Git tab: active worktree only, change summary, no branch list](40-git-tab-summary.md) | **new** | Confirm summary shape and per-file actions, then Core line counts |
| 39 | [Sessions list: no worktree header for a one-session worktree](39-single-session-worktree-header.md) | **new** | Find NSH's header; add the count rule in both |
| 36 | [Shared Rust build folder for worktrees of any project](36-shared-rust-builds.md) | **new**; this repo done | Decide cleanup, then Core env helper |
| 38 | [Ctrl+V pastes text too](38-ctrl-v-text.md) | **new**; TUI Ctrl+V reads images only | Fall back to clipboard text in the TUI |
| 37 | [`/continue`: show it at once](37-continue-feedback.md) | **new**; Core is fast, the wait is the model/network and the clients show nothing until `busy` | Measure the client path, then the pending row |
| 34 | [Network errors: subagents wait, never fall back](34-network-errors-no-fallback.md) | **new** | Classify network errors; wait on the same model |
| 33 | [Remove Git from the right sidebar](33-remove-git-from-right-sidebar.md) | **new** | Confirm only the right-sidebar section goes |
| 25 | [Alt+Enter: interrupt and send](25-interrupt-and-send.md) | **new**; reference in `nexus01` | Decide what happens to the queue; resolve the NSH `alt+enter` clash |
| 26 | [Launch: animate from the first frame, no progress bar](26-launch-splash.md) | **new** | Reproduce a cold daemon start |
| 28 | [Manager indicator and voice control](28-manager-indicator.md) | **new** | Confirm "voice on" means a live call |
| 27 | [Manager inspector](27-manager-inspector.md) | **new** | Design pass, then Core inspect endpoint |
| 5 | [Mobile client and the NSH look](05-mobile-and-design.md) | 5.1 implemented, unverified on device; 5.2 mobile redesign and 5.3 web/macOS look **new** | Answer the open questions in 5.2; audit screenshots for 5.3 |
| 6 | [ZMX mode](06-zmx.md) ([design](zmx-design.md)) | **Further than the old plan said:** M1 and most of M2 implemented offline in Core and both clients; isolation test fixed (daemon-start check still missing); docs stale; no live run | Fix the docs, D2 record; M0 live spikes need approval |
| 10 | [Fork subagent](10-fork.md) | Implemented; offline seed Core in `reports/10-fork/`; screenshots open (TUI dump mode cannot reach fork cards) | Capture parent and a fork interactively in both |
| 24 | [Harness workflow audit](24-harness-audit.md) | One observation item open | Observe larger heaps |
| 9 | [Backlog carried over](09-backlog.md) | Not scheduled | Pull an area into its own file when started |

Done: [4 tests and lints](done/04-tests.md), [8 provider usage table](done/08-provider-usage-table.md),
[22 remove `/continuation`](done/22-remove-continuation.md), [29 session commands](done/29-session-commands.md),
[30 questions](done/30-questions.md), [31 pending queue actions](done/31-pending-row-actions.md),
[32 worktree indicator](done/32-worktree-indicator.md), [35 timeline notice rows](done/35-timeline-notice-rows.md),
[1 release](done/01-release.md), [2 terminal](done/02-terminal.md), [3 Manager](done/03-manager.md),
[21 session status](done/21-session-status.md), [23 `/btw`](done/23-btw.md), [42 `/btw` parity](done/42-btw-modal-parity.md) (11-11; each keeps its unscheduled
hardware/approval-only checks).
Physical phone/touch was not verified.

Section 7 of the old plan (the backlog overview) is now the top of [09-backlog.md](09-backlog.md). The GPUI host spike
is in [decisions.md](decisions.md).

## Working tree on 2026-11-11

When this folder was made, another session had uncommitted work in the tree (terminal toggle: Core `terminal_api.py`,
TUI keys/layout/render, NSH header, manifest; `reports/terminal-toggle-2026-11-11/`). Its PLAN.md line ("Scout tool
budget") was carried into [landed.md](landed.md). That work is not part of this plan's audit.
