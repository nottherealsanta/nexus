# Native TUI refinement plan

## Intent

Make the native interface quieter and more coherent: compact session metadata,
a unified top bar, clickable composer controls, and a clean context/activity
footer. Keep the black conversation theme and the previously added Markdown
viewer, bounded modals, and cached provider usage.

## 1. Sessions and sidebar framing

- Show session metadata as a message count and compact age: `12 · 5m`,
  `3 · 2d`, or `0 · now`. Remove the words “message(s)” and “ago”.
- Remove the sidebar's `↵ open · ctrl+z undo` footer; keep the shortcuts functional.
- Retain compact two-line session entries and strengthen the sidebar boundaries.
- Full-height sidebars that push the entire center column aside are optional.
  Start with improved borders in the existing layout; expanding their height is
  worthwhile only if tabs, composer, resizing, and mouse targets remain coherent.

**Files:** `nexus/ui_support/session_status.py`,
`nexus/ui/ratatui/workflows.py` (`session_rows`),
`rust/tui/src/render.rs`, `rust/tui/src/render/chrome.rs`.

## 2. Unified top bar

- Give the entire tab/breadcrumb area one consistent gray background.
- Use a single black divider between tabs and the repository breadcrumb.
- Remove the extra black bands and redundant rules around that divider.
- Preserve readable active tabs, status, and the enlarged sidebar-toggle targets.

**Files:** `rust/tui/src/render/chrome.rs`, `rust/tui/src/render.rs`.

## 3. Composer context figures and activity line

- Right-align the context figures on the composer's last internal row.
- Use compact uppercase units with a useful decimal, such as `5.2K` and `1M`.
- Show used context, the standard-price context boundary, and the full context
  capacity when reported. Example: `5.2K / 200K / 1M · 0.5%`.
- Use the full context capacity as the percentage denominator. Omit unavailable
  price boundaries rather than inventing a second tier.
- Remove the word “Context”, percentage brackets, and `price ↑ at …` wording.
- Give the activity line the full available width below the composer. While
  working, animate its highlighted segment left-to-right and back, following
  Textual's meter behavior. Remove the separate spinner and “idle”/“working” text.
- Make the orange boundary indicator a small, thin tick instead of a tall mark.
- Keep the context figures and activity line clickable to open context inspection.

**Files:** `nexus/ui/ratatui/prototype.py`,
`rust/tui/src/{bridge,render,main}.rs`; references:
`nexus/ui_support/context.py`, `nexus/ui_support/tui_widgets.py` (`ActivityProgress`).

## 4. Root-agent selection

- Filter `/agent` suggestions, its picker, and `Shift+Tab` cycling to agents whose
  `contexts` includes `root`. Never cycle through subagent-only definitions.
- Use the same filtering rule for the new-session root-agent chooser.
- Keep the picker compact and anchored above the composer, with keyboard,
  mouse, search, and Escape support.
- Clicking the composer agent name opens this picker. Opening it does not create
  a session; session creation belongs to the explicit new-session flow.
- Preserve the host's root-agent lock after the first turn. Do not bypass prompt
  caching restrictions to change the root of an existing conversation.

**Files:** `nexus/ui/ratatui/{actions,prototype,workflows}.py`,
`nexus/ui_support/completion.py`, `rust/tui/src/{render,main}.rs`; reference:
`nexus/ui/tui/agent_picker.py` (already filters root-capable agents).

## 5. Clickable composer controls

- Agent name → root-agent picker.
- Model name or provider name → the same model-selection modal.
- Reasoning/effort value → the same picker as `/effort`.
- Derive click targets from the rendered control rectangles. Reuse command paths
  so mouse and keyboard share selection rules, host validation, and focus handling.

**Files:** `rust/tui/src/{render,main}.rs`,
`nexus/ui/ratatui/{actions,prototype}.py`.

## 6. Quieter conversation and new-session view

- Remove the repeated agent-name heading from root-agent responses. Keep agent
  identity in the composer and details panel, and retain child-agent/task identity.
- Remove the shortcut/hot-tip splash from new conversations for now. Keep
  commands and shortcut help available through their explicit entry points.
- Recalculate transcript gaps so removed headings and hints leave no empty blocks.

**Files:** `nexus/ui/ratatui/prototype.py` (`_project_turn`, empty-session blocks),
`rust/tui/src/transcript.rs`.

## Implementation order and acceptance

1. Settle top/sidebar framing and composer/footer geometry.
2. Unify root-agent filtering and wire composer click targets.
3. Remove redundant response labels and new-session hints.
4. Add focused regressions and compare wide/narrow terminal screenshots.

Verify mixed root/subagent lists, `Shift+Tab`, `/agent` completion, picker mouse
selection, explicit new-session creation, post-turn agent locking, model/provider
clicks, effort selection, preserved drafts, and focus restoration. Check context
formatting with no pricing tiers, a reported price boundary, missing measurements,
and large context windows; check idle versus animated activity without extra text.
Update `docs/ratatui-parity.md` and `plans/RATATUI_PLAN.md` with verified results.

## Current state

Implemented. Native Python tests (83), Rust tests (37, one ignored benchmark),
and the real-terminal input check pass. Sidebars retain the accepted existing-height
layout with stronger boundaries. See `docs/ratatui-parity.md` for verification.
