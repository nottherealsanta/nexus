# Native TUI design and interaction plan

## Goal

Make the native Ratatui client visually consistent and comfortable to use, with
Textual as the behavior reference. Keep the fully black theme, preserve visible
context, and make menus and usage views open without blocking the conversation.

## Implementation

### 1. Consistent black theme

- Make slash-command suggestions and `@` file-search popups match the main app:
  black backgrounds, consistent text colors, muted borders, and readable selection.
- Use shared palette tokens and border styles rather than colors specific to each popup.

**Files:** `rust/tui/src/render.rs`; references:
`nexus/ui/tui/theme.py`, `nexus/ui_support/tui_list.py`.

### 2. Modals and composer drawer

- Show the command palette in a bounded, searchable modal over the conversation.
- Show agent selection in a drawer anchored immediately above the composer.
- Support keyboard navigation, mouse selection, scrolling, Escape, and focus
  restoration. Clamp overlays to the available terminal size.
- Give panels an explicit presentation type so placement does not depend on titles.

**Files:** `rust/tui/src/{render,main,bridge}.rs`,
`nexus/ui/ratatui/{actions,workflows,prototype}.py`; references:
`nexus/ui_support/tui_command_palette.py`, `nexus/ui/tui/agent_picker.py`.

### 3. Bottom context and activity line

- Remove the repository path and branch from the bottom row.
- Adapt Textual's context meter and activity feedback into one bottommost line:
  context used/limit plus current activity, including loading and running states.
- Preserve the repository breadcrumb at the top and keep status readable at narrow widths.

**Files:** `rust/tui/src/{render,bridge}.rs`, `nexus/ui/ratatui/prototype.py`;
references: `nexus/ui/tui/app.py` (`_sync_activity`),
`nexus/ui_support/tui_widgets.py` (`ActivityProgress`), `nexus/ui_support/context.py`.

### 4. Message and composer spacing

- Give user messages a one-cell left inset.
- Replace thick blue bars with thin blue border glyphs for user messages and composer.
- Render turn numbers in muted gray with no background.
- Add one blank terminal row below the agent/model controls; terminal layout uses
  whole cells, so half-cell padding is not available.
- Account for these margins in wrapping, cursor placement, selection, and hit testing.

**Files:** `rust/tui/src/{transcript,render,main}.rs`; reference:
`nexus/ui/tui/app.tcss`.

### 5. Navigation and sidebar density

- Enlarge both sidebar-toggle click targets with padded icon areas.
- Reduce vertical gaps between session entries while retaining titles and metadata.
- Add a subtle horizontal divider between the tab bar and repository breadcrumb.
- Use the same layout rectangles for rendering and mouse targets.

**Files:** `rust/tui/src/{render,main}.rs`; reference:
`nexus/ui_support/tui_panels.py`.

### 6. Context and provider usage modals

- Audit the existing context inspection path and restore a complete context-usage
  modal, including its established shortcut and clickable meter entry point.
- Open provider usage immediately on `Ctrl+U` or `/usage`, showing the last fetched
  data. If no cached data exists, open with a clear loading state.
- Refresh asynchronously with a spinner, then update the same modal in place.
- Retain cached data on refresh errors, display freshness and error information,
  and provide manual refresh.
- Bound the cache and ignore stale responses after dismissal, session changes,
  or a newer refresh. Continue using host commands for all data access.

**Files:** `nexus/ui/ratatui/{actions,workflows,prototype}.py`,
`rust/tui/src/{bridge,render,main}.rs`; references:
`nexus/ui/tui/usage.py`, `nexus/ui_support/usage.py`,
`nexus/ui_support/tui_widgets.py` (`ContextDetailsScreen`).

**Observed:** native `/usage` awaits `providers_usage()` before opening its panel.
Context inspection already exists; the task is to complete its presentation and access.

### 7. Markdown document viewer

- Preserve file bodies and newlines when opening `AGENTS.md` from the context header.
- Route Markdown documents through the existing native renderer, with readable
  headings, paragraphs, lists, links, tables, and fenced code.
- Show file names and separate multiple included documents; support wrapping and scrolling.
- Keep literal content views, such as the system prompt, explicitly typed as plain text.

**Files:** `nexus/ui/ratatui/workflows.py` (`context_show`),
`nexus/ui/ratatui/{actions,prototype}.py`, `rust/tui/src/{bridge,render,markdown}.rs`.

**Observed:** `markdown.rs` already provides Markdown rendering; the context viewer
needs to preserve and route document content into it.

## Order and completion criteria

1. Establish shared theme and overlay presentation; update drawing and input together.
2. Refine message, composer, status, and sidebar layouts.
3. Complete context/provider usage and Markdown viewing.
4. Add focused regression coverage in the existing Ratatui projection, action,
   workflow, and PTY tests, plus Rust rendering tests and browser screenshots.

Verify keyboard and mouse behavior, focus restoration, scrolling, narrow-terminal
resizing, delayed/failed provider fetches, cached reopen, and multiline `AGENTS.md`.
Compare affected screens with Textual; unit tests alone do not establish visual parity.

Update `docs/ratatui-parity.md` and `plans/RATATUI_PLAN.md` with the resulting
contracts and verification status. Add a `docs/module-map.md` entry if a new Python
module is introduced. Preserve unrelated work in the checkout.

**Status:** implemented in the working tree. Focused Rust/Python/Textual tests,
PTY interactions, layering/docs checks, Ruff and terminal screenshots verified.
Focused web usage browser checks pass; the broad web harness stops on an existing
CSP evaluation issue before its usage section. Full cross-surface parity remains
outside these focused checks.
