# Plan 09: Back button in the subagent page top bar (Ratatui)

Status: implemented and fully verified (2026-10-20). Verified by Rust unit and PTY tests (`cargo test --manifest-path rust/tui/Cargo.toml --locked`: 590 passed, 2 ignored + 58 dashboard passed), Python test suite (`.venv/bin/python -m pytest` exact feature suite: 781 passed), and parent visual inspection of native captures in `artifacts/native-final`. Header normal and narrow show left back arrow `←`, title truncation, and status; click fixture returns to parent. Source: root `TODO.md` item 6
("top bar in subagent page should have a back button").

## 1. Intent

The Ratatui subagent page header gets a clickable `←` back control at its left
edge that does exactly what `Esc`/`Up` already do there: return to the previous
page (the parent agent page for a nested page, otherwise the root transcript).

### Non-goals

- Desktop (GPUI): paused (`AGENTS.md:8`); no desktop change is planned here.
- Web: already has Back on the subagent overlay (`docs/web.md:188`).
- Changing the rest of the header (title, model, status, details toggle).

## 2. Current evidence

- Header drawing: `draw_top_bar` on an agent page renders
  `  {title}{gap} {model} · {status} ▯  ` on one row
  (`rust/tui/src/render/chrome.rs:368-399`).
- Hit targets: on an agent page `tab_cells` returns only the details toggle
  (`chrome.rs:342-351`, hard-coded `width-5..width-2`), so there is no back target.
- Keyboard: `Esc`/`Up` send `action("dismiss", "")` on an agent page
  (`rust/tui/src/main.rs:1871-1876`).
- Python side: dismiss pops `agent_parents`, so nested pages go back one level
  (`nexus/ui/ratatui/workflows.py:117-118`, push at :541).
- Hover/pressed styling for header targets lives in
  `rust/tui/src/render/components.rs:~301`.
- Docs: native chrome hit-target conventions (three-cell padded targets, shared
  layout for drawing/hover/clicks) in `docs/ratatui-parity.md:1773-1780`.

## 3. Design

1. Add `TabHit::Back` (`chrome.rs:78`).
2. Factor the agent-page header into one layout function (like `top_layout`) that
   returns `(start, text, Seg)` runs: `" ← "` (`Seg::Back`), a one-cell gap,
   title, gap, model, status, details toggle, right padding. Both `draw_top_bar`
   and `tab_cells` use it, replacing the hard-coded details range, so drawing,
   hover and clicks cannot drift.
3. Click on `TabHit::Back` → `action("dismiss", "")`, the same path as `Esc`.
4. Style: muted like `Seg::Dashboard`/`Seg::Details`; hover/pressed like other
   header targets. `←` is in common fonts; no Nerd Font needed.
5. Narrow widths: the back target is kept before the title is truncated
   (title budget shrinks by the target width).

## 4. Tests and docs

- `chrome.rs` unit tests: `tab_cells` on an agent page returns Back at the left
  and Details at the right, both inside the width, matching drawn glyph cells;
  very narrow widths keep Back.
- Event test in `main.rs` (or the existing input tests): a click on the Back cell
  sends `dismiss`; nested pages return to the parent page (Python test in
  `tests/test_ratatui_workflows.py` for the pop order if not already covered).
- Docs: `docs/ratatui-parity.md` (native chrome section: agent page header has a
  back target), with "not visually verified" until a screenshot review.

## 5. Acceptance criteria

- [x] An agent page header shows `←` at the left; clicking it returns to the
      previous page, identical to pressing `Esc`.
- [x] From a nested agent page, Back returns to the parent agent page.
- [x] Details toggle still works and its hit target matches its glyph.
- [x] Rust and Python tests pass; visual review recorded and verified.

## 6. Risks and open decisions

- If [plan 07](07-root-only-subagent-spawning.md) lands, nested pages only occur
  for legacy sessions; the back stack must still work for them.
- Stray-glyph repaint logic keys on chrome geometry (`docs/ratatui-parity.md:1832-1837`);
  adding a target must not change geometry between frames of the same page.

## Final focused verification (2026-10-20)

- Tests independently verified by parent:
  - Python feature suite: `.venv/bin/python -m pytest` passed (781 passed).
  - `cargo test --manifest-path rust/tui/Cargo.toml --locked`: 590 passed, 2 ignored + 58 dashboard passed.
- Visual inspection: parent inspected native captures in `artifacts/native-final` (`review.html`, `artifacts/08-header-normal.png`, `artifacts/09-header-cjk-narrow.png`).
  - Normal and narrow CJK role/model headers visibly render the left back arrow `←`.
  - Title truncation cleanly accommodates the back arrow target even at narrow widths.
  - Status indicator remains aligned and visible.
  - Arrow click fixture emits the asserted native `dismiss` action and returns to parent page.
- Limitations: desktop (GPUI) paused; web already features Back on its overlay.
