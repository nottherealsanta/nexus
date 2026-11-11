# 21. Session status above the composer, not in the timeline

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

**Implemented (2026-11-09).** Both clients show a non-dismissible status strip immediately above the composer.
It uses the newest turn's summary (or live scratch until the summary arrives), newest outcome wins with an
additional-diagnostic count. It restores on revisit and clears while busy. Mobile reuses the strip without the
desktop inspector action.

- [x] **List:** errors/interruption, retry phases/exhaustion, recovery and cancelled waits leave the timeline.
  Directory/notification and ordinary wait-reply metadata is inspector-only. Model-visible recovery/notice messages
  and `via Manager` decisions remain in their ordered timeline positions.
- [x] **Lifecycle:** derived state, no manual dismissal; busy suppresses it immediately, including continuation,
  steer and queued-input turns. Successful later turns replace older failures.
- [x] **Actions:** applicable `/continue` plus desktop Inspect for the full HTTP status/body/history.
- [x] **Paired implementation and tests:** shared `interface/scenarios/session-status-btw.json`, failed/interrupted
  timeline exclusion, busy clearing and restoration; both client READMEs, API docs and porting ledger updated.
- [x] **Visual verification:** compare matching TUI/NSH status-strip captures. **11-11:** retaken from current code
  with the isolated `reports/sections21-23/visual_core.py` Core: `tui-status.png` (170×50), `nsh-desktop-status.png`
  (1440×900) and `nsh-phone-status.png` (390×844). Same strip, wording and `/continue` · Inspect actions in both.
