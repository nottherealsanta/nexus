# Plan 35 captures

Same seeded session on a scratch `NEXUS10_HOME` (no provider calls): one background
`build` child, one old failed `quick` notice without metadata, one lone `**Delegating
focused tests**` thought and one finished background shell.

- `tui-dark.png`, `tui-light.png`: real `nexus-tui --dump-frame` at 170x50, opened with
  `ctrl+k Subagent done demo enter`.
- `tui-dark-modal.png`, `tui-light-modal.png`: the `Subagent done` modal rendered by the
  TUI test harness from `interface/scenarios/subagent-done.json` at 170x50. The real
  binary capture raced a scratch-session tool-sync notice; the harness isolates the frame.
- `nsh-dark.png`, `nsh-light.png`: the NSH web build at 1440x900 against the same Core.
- `nsh-dark-modal.png`, `nsh-light-modal.png`: the same NSH session with the modal open.

The TUI `.txt`/`.svg` frames beside the main captures are the raw dumps used for the PNGs.
