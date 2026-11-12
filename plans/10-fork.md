# 10. Fork subagent verification follow-ups

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

Fork subagents are implemented in Core, TUI and NSH; completed work is recorded under [Landed](landed.md).

- [ ] **Visual verification:** side-by-side TUI/NSH screenshots from mock `fork_subagent` (not taken).
  Review the right sidebar's Agent row (still the parent's agent in both clients) and the inherited
  block's presentation (NSH has no tree lines beside the open rows).
  **11-11:** `reports/10-fork/visual_core.py` (run with `NEXUS10_DEV=1`, a scratch home and `NEXUS_PORT`) seeds a
  finished `mock/fork_subagent` turn with two forks, offline. Partly captured, see
  [reports/10-fork/README.md](../reports/10-fork/README.md): NSH parent with `2 forks` expanded and a fork child with the
  right sidebar (Agent row shows `build`). Not captured: the TUI child (its timeline is blank in both dump and
  interactive runs, not diagnosed) and a coloured TUI parent PNG; `--dump-frame --keys` does not expand the fork group
  (`alt+d` works interactively). This item stays open until the TUI pair is captured.
- [ ] **Ongoing observation:** watch parents re-tasking finished forks via `message_session`, terse task
  prompts and use across more models. This is monitoring, not unfinished implementation.
