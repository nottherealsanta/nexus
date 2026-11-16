# 10. Fork subagent verification follow-ups

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

Fork subagents are implemented in Core, TUI and NSH; completed work is recorded under [Landed](landed.md).

- [x] **Visual verification (2026-11-16).** Paired TUI/NSH captures in `reports/10-fork/` (`tui-child-sidebar-{dark,light}.png`,
  `nsh-child-inherited-open-{dark,light}.png`, `nsh-parent-forks-expanded-{dark,light}.png`), from `visual_core.py`.
  **Cause of the "blank TUI child":** not a decode error. Real Core JSON for a fork child decodes (the `null`
  `origin`/`raw` sit inside `parts: Vec<Value>`), and the TUI child does show its history; the dump was taken before
  the history arrived, because `--dump-frame --open <id>` waited for a *snapshot* and `Select` never bumps
  `snapshots_applied` (`main.rs`). It now waits for the opened session's own history. Hardening anyway: `Message` and
  `Notice` string fields accept `null`, and a failed message load is now reported in the timeline (`turns_error`)
  instead of silently dropped (`sync.rs` `load_history`). Test: `a_fork_childs_real_core_json_decodes_and_fills_the_timeline`
  on captured fixtures (`nexus-tui/tests/fixtures/fork_child_*.json`).
  **Decisions (D7):** the right sidebar's Agent row shows the parent's agent (`build`) in both clients (the TUI already did;
  the heading and cards keep the `fork` label in both). The inherited block: the TUI draws a `│` gutter beside its open
  rows; NSH now draws the same rail (`border-l`) instead of a bare indent.
- [ ] **Ongoing observation:** watch parents re-tasking finished forks via `message_session`, terse task
  prompts and use across more models. This is monitoring, not unfinished implementation.
