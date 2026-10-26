# Plan 14: Shell tool rows: 2 input lines, a 4-line output window, full input in a modal (Ratatui)

Status: proposed (2026-10-25). Nothing in this plan is implemented. Source: root `TODO.md` item 7,
quoted unchanged:

> - Collapsed tools need to render better. For example, the shell tool, when uncollapsed, the first two lines should show the input, the 4 lines (scrolling) output. When clicked on input it should show the full input in a modal just in case if it's too big if it's more than say four lines.

## 1. Intent

An opened (uncollapsed) shell tool row shows:

1. **Input**: at most 2 rows of the command.
2. **Output**: a window of 4 rows. While the command runs it follows the newest output (it
   "scrolls"); when done it shows the last 4 rows.
3. Clicking the input opens a modal with the **full** command when the command is longer than
   4 lines (the TODO's threshold) or was clipped to 2 rows. Nothing is lost: clipping is announced
   (`AGENTS.md:120-122`).

The same compact pattern is then offered to other tools in a later step (Q3), not in this plan.

### Non-goals

- The closed (collapsed) row stays `Bash <command>` clipped to 40 cells (`docs/ratatui-parity.md:188-190`).
- The full labelled details (`i`, `/verbose`, `tool_details`) stay as they are.
- Desktop paused; web deprecated.

## 2. Current state (checked 2026-10-25)

- Projection: `tool_signal` builds shell rows as **every** command line (`code` rows, `$ ` then
  indented continuation) followed by **every** output hit (`out` rows), then a failure row, bounded
  only by the global signal bound (`nexus/ui_support/tool_signal.py:314-321`, `:348-357`;
  `MAX_SIGNAL_CHARS = 100_000`, `:31`). The job header is stripped (`strip_job_header`, `:161`).
- Drawing: Rust draws signal rows behind the `│` rail with no per-section limits
  (`rust/tui/src/transcript/tools.rs:47-62`, `:152-173`; row kinds in `transcript/signal.rs`).
- Grouped members open as `┌ head 0.8s` with rows behind `│` closing in `╰`
  (`docs/ratatui-parity.md:188-196`).
- Long outputs: a row whose output exceeds 8 lines gets `N lines ▸` and expands in place
  (`block_toggle` on `<call_id>:output`, kept in `shell.expanded`) (`docs/ratatui-parity.md:735-737`).
- The generic modal is `shell.show(title, body)` (used e.g. for file previews,
  `nexus/ui/ratatui/sidebar.py:309`). Operations the client may send are allow-listed per snapshot
  (`nexus/ui/ratatui/prototype.py:1598-1606`).

## 3. Assumptions

- A1. "the first two lines should show the input, the 4 lines (scrolling) output" is read as a
  fixed layout: ≤ 2 input rows, then a 4-row output window. "Scrolling" means the window follows
  the tail while output streams. Mouse-wheel scrolling *inside* the window is Q1 (a nested
  scroll region inside the transcript is a larger change).
- A2. "more than say four lines" is the modal threshold for the input (> 4 logical lines), and the
  modal is also offered whenever the 2-row preview clipped anything.
- A3. Applies to the shell tool family (`SHELL_TOOLS` in `tool_signal.py`), both standalone rows and
  verb-group members.

## 4. Design

### 4.1 Projection (`nexus/ui_support/tool_signal.py`, `nexus/ui/ratatui/prototype.py`)

Add a structured shell section to the signal instead of flat rows (keep the flat `rows` for other
surfaces and for `/verbose`):

```
shell = {
  "input": ["$ git -C /repo status", "  --short"],     # ≤ 2 display lines, already escaped
  "input_lines": 7,                                   # total logical lines of the command
  "input_clipped": true,                              # anything not shown in "input"
  "output_tail": ["…", "…", "…", "…", "…", "…"],      # last ≤ 64 lines (bounded), escaped
  "output_lines": 312,                                # total lines seen
  "failure": "exit 2" | "",
}
```

- `input` uses `strip_workspace` as today; long single lines are not cut in Python (Rust wraps and
  clips to 2 display rows and must report clipping by drawing `…`).
- The block carries `input_operation: {"kind": "tool_input", "call_id": "<id>"}` when
  `input_lines > 4` or `input_clipped`. Add it to `allowed_operations`.
- `tool_input` handler (in `nexus/ui/ratatui/workflows.py` operation dispatch): looks up the tool
  call in the current view (or the agent page view), and calls
  `shell.show("Shell input", <full command, control-escaped>)` with a header line
  `N lines · M characters` and, if the projection had clipped characters (`cuts`), the clip notice.
  No host call: the tool input is already in the reduced view.

### 4.2 Drawing (`rust/tui/src/transcript/tools.rs`, `transcript/signal.rs`)

- Opened shell body:
  - row 1–2: input (`code` style); if clipped, the last visible cell is `…` and a muted
    ` · N lines ▸ view` suffix that carries `input_operation` (click target).
  - rows 3–6: output window. While `status == "running"`: the last 4 rows of `output_tail`
    (follows the tail as snapshots arrive). Done: the last 4 rows. Above the window, when
    `output_lines > 4`: a muted `↑ K earlier lines` row carrying the existing `output_operation`
    (expands the full output in place, the current `N lines ▸` behaviour) so nothing is hidden.
  - failure row (red) after the window, as today.
- Row heights are fixed (2 + up to 1 + 4 + failure), so streaming output never makes the transcript
  jump: this is the main visual win.
- Under `/verbose` the old full rendering is kept.

### 4.3 Running output

Check before implementing whether a running shell call's partial output reaches the view (look for
progress/partial events reduced into `ToolCallView` in `nexus/view/reduce.py`). If partial output is
not streamed, the window shows `running…` until the result arrives and the "scrolling" part of A1
is Q2 (needs host-side progress events; out of scope here).

## 5. Implementation steps

1. Confirm §4.3.
2. Python: `tool_signal` shell section + projection field + `tool_input` operation + handler.
3. Rust: deserialize the new field in `bridge.rs` (`Content`), draw per §4.2, wire the click.
4. Docs: `docs/ratatui-parity.md` (tool rows section `:180-196`, `:735-744`) and `docs/surfaces.md`
   if the shape is meant to be shared.

## 6. Tests and acceptance

- Python (`tests/test_tool_signal*.py` or the closest existing file; find with
  `rg "tool_signal" tests`): 1-, 2-, 5-line commands; clipping flags; output tail bound; failure
  row; `strip_workspace` still applied; `tool_input` opens a modal with the full text; operation not
  offered when the command is short.
- Rust: an opened shell row is exactly 2 + 4 rows (+ hint rows); a running row with growing output
  keeps its height; `↑ K earlier lines` present only when needed; click on the input hint sends
  `tool_input`; narrow widths.
- PTY: a long command opens the modal; Esc closes it.
- Acceptance:
  - [ ] Opened shell rows show ≤ 2 input rows and a 4-row output window, with every omission announced.
  - [ ] A long command opens in a modal from the input row.
  - [ ] The transcript does not jump while a shell command streams output (if streaming exists).

## 7. Risks and open questions

- Q1. Wheel scrolling inside the 4-row window (nested scroll region): wanted?
- Q2. Live streaming of shell output into the transcript, if not already reduced.
- Q3. Apply the same input/output window to other tools (Read, Grep, MCP calls)?
- Risk: four rows can hide the one line that matters (an error in the middle). The failure row and
  `↑ K earlier lines` keep it one click away; `i` still opens the full details.
