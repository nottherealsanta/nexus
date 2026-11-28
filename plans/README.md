# Nexus plans

The plan used to be a single `PLAN.md` at the repo root. Since 2026-11-11 it is this folder: one file per piece of
work; `PLAN.md` was removed. The plan before 2026-11-06 is kept whole in
[docs/archive/PLAN-2026-11-06.md](../docs/archive/PLAN-2026-11-06.md). **Section numbers quoted by older docs and code
comments ("PLAN.md 8.2", "14.1") refer to that file.** Section numbers below (1–40) are the ones from the
2026-11-06 plan where they existed, so "section 6" still means external workers (formerly ZMX). Current behavior is in
[docs/README.md](../docs/README.md); deliberate changes from old Nexus are in the
[porting ledger](../docs/porting-ledger.md).

## Rules every item keeps

- Preserve the visible, ordered context timeline: everything the model receives is shown in order, the UI shows nothing
  the model does not have, wire events are delivered live, deletion and clipping are announced. Exceptions: the Manager's
  own context ([docs/manager.md](../docs/manager.md)) and external worker children ([zmx-design.md](zmx-design.md) section 4.3).
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
| 55 | [Direct Claude subscription mode](55-claude-direct.md) | **experimental functional support landed 2026-11-28**; Haiku OAuth/tools/cache/version/gzip verified; Sonnet 5.5 medium native `read` loop passed after user-approved visible OMP compatibility instruction resolved 429; Nexus headers/auth unchanged | Actual signed prefix-binding and long-context checks; multi-account/long-lived grants, deferred tools, volatile scheduling, server context and cache-write ledger remain |
| 54 | [Standalone audio recorder](54-standalone-recorder.md) | **paused 2026-11-27 at user request**; partial Core and client implementation is uncommitted, no milestone is ticked, and the shared native spool extraction is unfinished and currently fails to compile | See the handoff in §8: finish/review the shared spool, fix NSH cross-tab recovery races, integrate native persistence, then run paired and consented real-hardware checks |
| 51 | [Firecrawl Local: scrape, documents, map, crawl](51-firecrawl.md) | **landed 2026-11-23** in Core and both clients; **release source changed 2026-11-24** to the pinned 0.1.1 public build release (SHA256SUMS-verified; Nexus CI no longer builds or serves it) | Verify the external `v0.1.1` assets (none published when checked 11-24), then a real install and live scrape; JavaScript-page and Linux checks; NSH tests and paired screenshots |
| 52 | [First-run onboarding](52-onboarding.md) | **landed 2026-11-23**; Web search step added to desktop onboarding 2026-11-24 (TUI implemented, NSH pending) | Wire NSH's search step; user tries it on a fresh home; real sign-in from the tour unverified |
| 50 | [Core performance: Python first, Rust for proven hot spots](50-rust-core.md) | **new 2026-11-17**; trivial routes waited up to 1.3 s on Core's event loop; five workstreams (A–E) in worktrees | Merge workstreams; user restarts the daemon and collects a week of `nexus perf` |
| 49 | [Markdown links](49-markdown-links.md) | **partial 2026-11-17** | Extend link clicks beyond assistant timeline Markdown; verify both clients |
| 48 | [Context header: draft, then fixed](48-context-header-modes.md) | **landed 2026-11-17** except screenshots | Paired TUI/NSH screenshots in `reports/48-context-header-modes/`; try a real session with the switch on |
| 46 | [Adaptive orchestration and handoffs](46-adaptive-orchestration.md) | **prompts landed 11-13**; 60 offline tests pass | Observe scope, acceptance and repeated discovery on subsequent user tasks; improvement not yet measured |
| 44 | [Tooling and prompts from a session review](44-session-review-tooling.md) | **landed 2026-11-12** except background-job pipe status | Re-run its measurement queries after a week of use; revisit triggers are in the file |
| 43 | [Use ghostty-web for NSH terminals](43-ghostty-web-terminal.md) | **new** | Spike WASM packaging, API compatibility and web/macOS WebViews |
| 37 | [`/continue`: show it at once](37-continue-feedback.md) | **new**; Core is fast, the wait is the model/network and the clients show nothing until `busy` | Measure the client path, then the pending row |
| 34 | [Network errors: subagents wait, never fall back](34-network-errors-no-fallback.md) | **new** | Classify network errors; wait on the same model |
| 28 | [Manager indicator and voice control](28-manager-indicator.md) | **mostly landed** | Screenshots of the four states; verify NSH indicator box on each target |
| 27 | [Manager inspector](27-manager-inspector.md) | **new** | Design pass, then Core inspect endpoint |
| 5 | [Mobile client and the NSH look](05-mobile-and-design.md) | 5.1 implemented, unverified on device; 5.2 mobile redesign and 5.3 web/macOS look **new** | Answer the open questions in 5.2; audit screenshots for 5.3 |
| 6 | [External workers (formerly ZMX)](06-zmx.md) ([design](zmx-design.md)) | **Renamed 11-23: `PTY · Claude Code`, `ACP · Antigravity`, `CLI · Antigravity` for legacy rows, `[workers]` config with `[zmx]` aliases; Antigravity ACP implemented; live Nexus E2E and TUI/NSH approval-card and denial-attention parity passed 11-23** (real approval API deny, fresh-process resume, usage recorded; short and long cards match, `reports/acp-approval-parity-2026-11-23/`; full `Needs attention` text wraps in both, `reports/zmx-attention-parity-2026-11-23/`); 194 offline ZMX tests before the rename; Claude Code unchanged | Mobile approval card; daemon restart; full NSH scenario `Connection reset by peer` flake; M0 Claude spikes; inspector rebuilt requests; vendor terms need a written answer |
| 10 | [Fork subagent](10-fork.md) | Implemented; NSH captures in `reports/10-fork/`; TUI child blank in the TUI (not diagnosed), so the visual item stays open | Diagnose the blank TUI child timeline, then capture the TUI pair |
| 24 | [Harness workflow audit](24-harness-audit.md) | One observation item open | Observe larger heaps |
| 9 | [Backlog carried over](09-backlog.md) | Not scheduled | Pull an area into its own file when started |

Done: [39 worktree header](done/39-single-session-worktree-header.md), [45 tabs and splits](done/45-tabs-and-splits.md), [26 launch splash](done/26-launch-splash.md), [47 questions in the composer slot](done/47-question-in-composer.md), [4 tests and lints](done/04-tests.md), [8 provider usage table](done/08-provider-usage-table.md),
[53 question choice UX](done/53-question-choice-ux.md),
[22 remove `/continuation`](done/22-remove-continuation.md), [25 interrupt and send](done/25-interrupt-and-send.md), [33 right-sidebar Git removed](done/33-remove-git-from-right-sidebar.md), [29 session commands](done/29-session-commands.md),
[30 questions](done/30-questions.md), [31 pending queue actions](done/31-pending-row-actions.md), [40 Git tab summary](done/40-git-tab-summary.md), [41 terminal separation](done/41-terminal-user-only.md),
[32 worktree indicator](done/32-worktree-indicator.md), [35 timeline notice rows](done/35-timeline-notice-rows.md),
[1 release](done/01-release.md), [2 terminal](done/02-terminal.md), [3 Manager](done/03-manager.md),
[21 session status](done/21-session-status.md), [23 `/btw`](done/23-btw.md), [42 `/btw` parity](done/42-btw-modal-parity.md), [36 shared Rust builds](done/36-shared-rust-builds.md), [38 Ctrl+V text](done/38-ctrl-v-text.md) (11-11; each keeps its unscheduled
hardware/approval-only checks).
Physical phone/touch was not verified.
Plans 40 and 41 lack paired screenshots; plan 41's inspector job lane is tracked in [backlog 9.18](09-backlog.md).

**Still being discussed:** [planning/](planning/README.md) holds ideas that are not yet plans (not numbered, not scheduled).
Today: the `nexus.santa.build` install script and docs site, and [multiplayer](planning/multiplayer.md) (voice, and sharing a session or project).

Section 7 of the old plan (the backlog overview) is now the top of [09-backlog.md](09-backlog.md). The GPUI host spike
is in [decisions.md](decisions.md).

## Working tree on 2026-11-11

When this folder was made, another session had uncommitted work in the tree (terminal toggle: Core `terminal_api.py`,
TUI keys/layout/render, NSH header, manifest; `reports/terminal-toggle-2026-11-11/`). Its PLAN.md line ("Scout tool
budget") was carried into [landed.md](landed.md). That work is not part of this plan's audit.
