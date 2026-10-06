# Context header toggles and quieter tool rows

Status: proposed (2026-10-02). Native Ratatui client only. Web is not touched
(to be deprecated).

## Goals

1. Each context-header chip (`System prompt`, `Tools`, `AGENTS.md`, `Skills`,
   `MCP`) opens its own section when clicked.
2. Before the first turn, tools, skills and MCP servers can each be switched off
   and back on from that section.
3. `Skills` and `MCP` show two counts, project then global, unlabelled; the
   project count is greyed out.
4. A running subagent row has one blank line above and below it, and shows only
   its most recent tool call.
5. Tool groups lose the `✓` / `✗` marks and the `· N failed` suffix. A failed
   call no longer colours its group.

## Current state

| Area | Where | What happens today |
| --- | --- | --- |
| Header strip | `nexus/ui/ratatui/prototype.py` `_compact_header` | One `context_header` block whose `operation` is `context_menu`, so any click opens the "Context sections" menu, not a section. |
| Chip hit-test | `rust/tui/src/main.rs:1579` | Uses `r.context`, which is never laid out (`Rect::default()` in `render.rs:674`). It also splits the strip into five equal columns, which does not match chips of different widths. |
| Section dialogs | `nexus/ui/ratatui/workflows.py` `context_show`, `tools_modal`, `context_extensions` | System / AGENTS.md / Tools / Skills / MCP views exist. Skills and MCP can be toggled. Tools are read-only. |
| Toggle storage | `Session.disabled_extensions` (`nexus/session/session.py:717`), `context.extension_selected` events, `ContextExtensionSelect` (`nexus/host/facade.py:1622`) | Only `skills` and `mcp` categories. Locked once `turn.started` exists. |
| Tool filtering | `Runtime._selected_manifest` (`nexus/runtime.py:3033`) | Drops the tools of disabled MCP servers. No per-tool filter. |
| Counts | `_compact_header` → `Tools 13`, `Skills 1`, `MCP 0` | One total. `scope_counts` (`ui_support/context_header.py:87`) already splits rows by `scope == "project"`, in the section body text. |
| Tool group row | `rust/tui/src/transcript.rs:520` `tool_group` | `✗` + red tone + `· N failed` when any member failed, `✓` otherwise. Members that failed are red. |
| Subagent row | `prototype.py` (Task branch) + `timeline._task_child_activity` | `✓ Task · …` with `N tool calls · 6m 53s`. While running, it shows the last **two** calls joined by `→`. No extra gap around it. |

## Plan

### 1. Clickable chips (Ratatui)

- `_compact_header` keeps one strip but gives each member chip its own
  `operation: {"kind": "context_show", "key": <key>}`. The strip-level
  `context_menu` operation stays as the keyboard fallback (Enter on the strip).
- In Rust, compute chip cells the same way the tab bar does (`render::top_cells`):
  add `render::context_cells(block, area) -> Vec<Cell { start, end, row, key }>`
  that follows the same wrapping as `transcript.rs` `context_header`.
- Replace the `r.context` branch in `main.rs` with a hit-test on the
  transcript row: when the clicked row belongs to the `context:header` block,
  resolve the chip under the column and send
  `{"type":"context_header","key":…}`. A click between chips does nothing.
- Hover/focus: the chip under the mouse gets the agent colour on its label
  (not only the `▌` bar) so it reads as clickable.
- Agent pages (`agent_page`) reuse the same chips; `context_show` already reads
  `agent_context` there. Toggles are disabled on agent pages (read-only).

### 2. Toggle tools, skills and MCP before the first turn

Host contract (rule 4: all UI work goes through the host):

- Extend the `ContextExtensionSelect.category` domain to
  `"tools" | "skills" | "mcp"` in `nexus/host/protocol.py`; validate in
  `facade.py` against `context["tools"]` names for `tools`.
- `Session.disabled_extensions` gains a `"tools"` set (names compared exactly,
  like MCP). Replay of `context.extension_selected` covers it with no new event
  type (rule 5).
- `Runtime._selected_manifest` also removes `disabled["tools"]` from
  `manifest.tools`. The early return checks all three sets.
- `inspect_context` marks each tool row with `enabled: bool` (and
  `config_enabled` where Settings disabled it), matching skills/MCP rows, so the
  preview and the token totals reflect the selection.
- Keep the existing lock: after the first `turn.started` (or while active) the
  facade refuses with the current message. The message text adds "tools".
- Open question to confirm: should the agent definition's own tool allowlist
  hide tools from this list entirely, or show them as `· not in agent`? Default
  in this plan: hide them; they are not offered to the model anyway.

Ratatui UI (`workflows.py`):

- `tools_modal`: each tool row gets a leading state mark (`●` on, `○` off,
  greyed text when off). Enter / click toggles via a new
  `tools_select` operation that calls `select_context_extension(session,
  "tools", name, enabled)` and re-renders. `→` / the family row still expands;
  a separate key (`d` or `Space`) opens the full definition so toggling and
  inspecting do not collide. A family row toggles all of its tools.
- `context_extensions` (skills, MCP): same `●`/`○` marks instead of
  `· enabled` / `· disabled` text, same key behaviour.
- When locked, rows render without marks and the existing
  "Context locked after first turn" note stays at the top.
- Header counts reflect only enabled entries; a disabled count is never hidden,
  it is shown in the section dialog (`Tools · 11 of 13 on · ~3.6K tokens`).

Native workflows call `select_context_extension` for skills/MCP and tools.

### 3. Project / global counts for Skills and MCP

- `_compact_header` emits, for `skills` and `mcp`, `counts: [project, global]`
  instead of folding a number into `title`. Use the existing `scope` field on
  each row (`scope == "project"` → project, anything else → global), counting
  only enabled rows.
- Rust `context_header` renders `Skills 1 3 ~103`: first number in `p.quiet`
  (greyed, project), second in `p.muted` like the rest of the chip (global). No
  labels; the order is fixed and documented in `docs/ratatui-parity.md`.
- Zero is still shown (`MCP 0 0`) so the two positions stay stable.
- The section dialogs group rows under `Project` and `Global` headings, so
  the meaning is visible in full where there is room.
- `Tools` keeps one count.

### 4. Subagent rows: spacing and latest call only

- `prototype.py`: give the Task block `gap` ≥ 1 above, and make the next block's
  gap ≥ 1 (`bottom = 1` in the entry tuple), whether running or finished. The gap
  merge loop already takes the max, so two adjacent Task rows get one blank line
  between them, not two.
- `timeline._task_child_activity`: `tools[-1:]` instead of `tools[-2:]`, no
  `→` join. Format as `<Tool> <target>` (same `tool_heading` style as group
  rows, e.g. `Grep pattern=… path=nexus/ui`), plus the latest progress line
  when present, truncated to the row width in Rust rather than to a fixed 90
  characters.
- Row look:
  ```
                                                   (blank)
    ⠋ Task · Agent reuse          134 calls · 6m 53s
      Read nexus/ui/ratatui/actions.py (offset=1, limit=32)
                                                   (blank)
  ```
  Spinner while running, agent colour on `Task`, the latest-call line in
  `p.muted`, metrics right-aligned when they fit. Finished: no glyph, the
  second line becomes the metrics (`134 tool calls · 6m 53s`).

### 5. No success/failure marks on tool groups

- `transcript.rs` `tool_group`: drop `✓`, `✗` and the `· N failed` suffix. The
  glyph column shows the spinner while running and a blank otherwise; the count
  stays (`18 Grep …`). Tone is the agent colour while running, `p.muted`
  otherwise. Failures no longer change the group's colour.
- Expanded members: a failed member is not coloured red either; its detail
  (the `Error` section from `tool_detail_sections`) still shows the error text in
  full when opened. Hiding the mark is fine. Hiding the error output is not
  (AGENTS.md: never hide what the agent saw).
- Same for the single `tool` row and the Task row: no `✓`/`✗`.
- `prototype.py`: `status` becomes `running | done`; stop sending `failures`
  (remove the field from the Rust `Content` struct, or ignore it, in the same
  change).
- Turn-level errors (`turn.error`, the `Error:` block) are unchanged; only
  per-tool failure marks go.

## Tests

- `tests/test_host_facade.py`: `ContextExtensionSelect` with `category="tools"`
  disables a tool, it disappears from `inspect_context` and from the manifest
  sent to the scripted provider; refused after the first turn; unknown tool
  name rejected.
- `tests/test_session_*`: replay of tool selection events reproduces
  `disabled_extensions["tools"]`.
- `tests/test_ratatui_actions.py`: chip click with each key opens the matching
  section; `tools_select` round-trips; header counts emit `[project, global]`.
- Rust unit tests in `transcript.rs`: replace
  `gaps_context_chips_and_tool_failures` with checks that a failed group renders
  no `✗` / `failed`, the two counts render with the project one in `quiet`, and
  `context_cells` hit-tests match the rendered chip positions at narrow and wide
  widths.
- Task row: one latest call, blank line above and below.
- `tests/playwright_ratatui_check.py` for the toggle list and group rows.

## Docs to update in the same change

- `docs/ratatui-parity.md`: chip clicks, count order, tool-row marks.
- `docs/tools.md`: per-session tool selection and the lock.
- `docs/context.md`: selection affects the assembled prompt and the cache key.
- `docs/host.md`: `ContextExtensionSelect` categories.
- `docs/decisions.md`: "tool failures are not marked in the timeline: the loop
  recovers on its own; the error stays readable in the expanded detail".

## Not verified

- That every skill/MCP row carries `scope` (`runtime.py:2797` builds skill rows
  and should; MCP rows need checking).
- How tool toggles interact with tools that a skill or the loop requires
  (`ReadMcpResource`, `Task`). Proposal: allow all, but refuse to turn off the
  last tool when the model needs at least one.
