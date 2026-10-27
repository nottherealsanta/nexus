# Plan 12: Subagent page looks like the root page, with a two-column back rail (Ratatui)

Status: implemented and offline/PTY verified; pixel/contrast acceptance remains open (2026-10-26). Source: root `TODO.md` item 5,
quoted unchanged:

> - subagent screen/modal should look like the root agent except that there is back button which is the first two cols of TUI ( with a <-  in the center )

## 1. Intent

Opening a subagent shows the same page chrome as a root conversation (top bar, transcript
styling, composer card, details sidebar, bottom bar). The only addition is a **back rail**: the
two leftmost terminal columns, full height of the page, with `←` drawn in the vertical centre.
Clicking anywhere on the rail goes back (same as `Esc`/`Up` today).

### Non-goals

- No way to type to a subagent: there is no host command for a user message to a child
  (`nexus/host/protocol.py:695-730` has `AgentCancel`, `AgentClose`, `AgentTranscript` only). The
  composer stays read-only (open question Q1).
- Desktop paused, web deprecated (`AGENTS.md:8`, `:109`).
- The transcript projection of child turns already reuses the root's `_project_turn`
  (`nexus/ui/ratatui/prototype.py:1164-1188`); no projection rewrite.

## 2. Current state (checked 2026-10-25)

| Piece | Today on an agent page | Where |
| --- | --- | --- |
| Snapshot | `agent_page=<id>`, `tabs=[]`, `sessions=[]`, `prompt=None`, `queue_lines=[]`, own context meter and details panel | `prototype.py:1415-1466` |
| Top bar | one row: `←` back target (plan 09), title, model, status, details toggle | `rust/tui/src/render/chrome.rs:404-449` (`agent_layout`, `TabHit::Back`) |
| Composer | 3 rows, the text `Sub agent · read-only · Esc or ↑ returns to the parent` | `rust/tui/src/render.rs:1767-1769`, `:2480-2486` |
| Project strip | absent | `render.rs:1843` |
| Queue rows / controls | disabled | `render.rs:268-277`, `:1398-1427` |
| Live pane | hidden | `rust/tui/src/live_bar.rs:36`, `:49` |
| Back | `Esc`/`Up` → `dismiss`; nested pages pop `agent_parents` | `rust/tui/src/main.rs:2054`, `:2312`; `nexus/ui/ratatui/workflows.py` (pop) |
| Bottom padding | 30% of the viewport below the last row | `docs/ratatui-parity.md:741-744` |

The page already *is* largely the root page; what makes it look different is the stripped chrome
(1-row header, small read-only strip instead of the composer card, no project strip).

## 3. Assumptions

- A1. "the first two cols of TUI" means terminal columns 0–1 of the whole page, from the row under
  the top bar to the bottom bar, not just the header row.
- A2. "look like the root agent" means the same chrome *shapes*; disabled controls are drawn
  muted, not hidden, so the layout does not jump when returning to the root.
- A3. The existing header `←` (plan 09, `plans/planning/09-subagent-page-back-button.md`) is kept or
  removed by Q2; this plan defaults to **removing it** so there is one back affordance.

## 4. Design

### 4.1 Geometry

- `render::regions` (`render.rs:1798`) gets a `back_rail: Rect` = `x = area.x, width = 2`, from the
  first row below the top bar to the last row above the bottom bar, only when
  `!s.agent_page.is_empty()`. Every other region on the page starts at `x + 2` and is 2 columns
  narrower. Because `regions` is shared by drawing and hit-testing, both move together.
- Very narrow terminals (conversation < 24 columns after the details drawer): drop the rail and
  keep the header `←` as fallback, so the page never becomes unusable.

### 4.2 Drawing

- Rail background `p.panel` (same as the header), a one-column `│` border in `p.border` on column 1,
  and `←` in `p.muted` at column 0, row `rail.y + rail.height / 2`. Hover: `p.element` background
  on both columns and `p.text` arrow (the hover/pressed pattern in `render/components.rs`). Under
  `NEXUS_ASCII=1` use `<`.
- Top bar on the page: the root's `top_layout` style (title left, model/status, right buttons)
  without session tabs; the manager/sessions buttons are drawn muted and inert (A2).
- Composer: the root composer card shape (`render.rs:2487+`) with the editor disabled and the
  placeholder `Sub agent · read-only · Esc, ↑ or ← returns`; height = the root minimum
  (`composer_height` path for an empty draft), not 3.
- Project strip: shown, read-only, with the child's root/worktree if the snapshot has it, else the
  parent's (the child shares the session checkout, `docs/decisions.md:101-104`).

### 4.3 Input

- New `TabHit`-like hit for the rail (or a `RailHit::Back` in the page hit-test) → `action("dismiss", "")`,
  the exact path of `Esc`. Mouse down on the rail must not start a transcript text selection.
- Keyboard unchanged.

### 4.4 Stray-glyph and cache safety

Adding columns changes page geometry. The page keeps one geometry for its lifetime (rail always
present on a page at a given width), and the root page never has it, so the repaint rules keyed on
chrome geometry (`docs/ratatui-parity.md`, native chrome section) still see a stable layout per
page. The per-page laid-out row cache (`docs/ratatui-parity.md:743-744`) is keyed by width; the
narrower width must be part of the key so returning to the root does not reuse page wraps.

## 5. Implementation steps

1. `render.rs`: `Regions.back_rail`; shift page regions; narrow-width fallback.
2. Draw the rail and the root-style top bar/composer on agent pages (`render/chrome.rs`, `render.rs`).
3. Hit-test the rail in the page click handler (`main.rs`), send `dismiss`.
4. Remove `Seg::Back` from `agent_layout` unless Q2 keeps it; update plan 09's tests accordingly.
5. Docs: `docs/ratatui-parity.md` (subagent page section, `:735-744`) describes the rail and the
   root-like chrome; mark "not visually verified" until screenshots are reviewed.

## 6. Tests and acceptance

- Rust unit: `regions` on an agent page has a 2-column rail and every other region starts at `x+2`;
  root page has none; narrow fallback; `←` drawn at the vertical centre for odd and even heights;
  ASCII mode; hover style.
- Rust event test: click on any rail row (top, centre, bottom) sends `dismiss`; a click at column 2
  does not.
- Python: `tests/test_ratatui_workflows.py` nested pages still pop one level per back.
- PTY (`tests/test_ratatui_pty*.py` pattern): open a subagent page, click column 0 mid-height,
  assert the root transcript returns.
- Acceptance:
  - [ ] A subagent page has the root's header, composer card (read-only) and bottom bar.
  - [ ] Columns 0–1 are a full-height back rail with `←` centred; clicking it returns like `Esc`.
  - [ ] No layout jump or stray glyphs when going back and forth (visual review recorded).

## 7. Risks and open questions

- Q1. Should the subagent composer accept input (steer the child)? Needs a new host command and a
  loop path to deliver a user message to a running child; out of scope here.
- Q2. Keep the header `←` from plan 09 as well, or only the rail? Default: only the rail.
- Q3. Should the 30% bottom padding of the page become the root's bottom buffer for consistency?
  (`docs/ratatui-parity.md:204-209` vs `:741-742`.) Default: use the root's rule.
- Risk: a 2-column rail is easy to hit by accident when selecting text from the left edge; the rail
  must not start a selection, and selection must start at column 2.

### Geometry follow-up

Child composer now uses the empty-root seven-row minimum rather than the old
three-row strip. Rust geometry coverage checks the child rail and composer.
Full Rust suite passes; native visual/chrome parity acceptance remains pending.

### Finisher native acceptance

Added native PTY child back-rail click -> dismiss; same process then verifies root
vertical-tab open/close/new actions. The test passes. Root-like full chrome visual
review remains unverified; do not interpret action routing as visual approval.

### Native capture inspection

Captured 40/140-column real native PTYs to ignored
`artifacts/planning-acceptance/child-{40,140}.ansi/.txt`; asserted child title and
read-only composer in each. Inspected textual terminal captures: centred back rail
is present, but the header still has a second `←` affordance. This conflicts with
the plan's default single-affordance intent; no claim of full visual acceptance.
Narrow composer clips its hint. Text capture is not pixel/contrast approval.

### Duplicate-back and narrow-hint correction

Removed the duplicate header back glyph/hit target only while the dedicated rail
is reserved. Narrow (<26 column) fallback keeps the header back control; keyboard
back remains unchanged. Composer chooses a complete width-appropriate instruction.
Real PTY tests at 24/40/140 columns assert exactly one left arrow, complete hint,
and Escape dismissal; all pass. Pixel/contrast review remains unverified.
