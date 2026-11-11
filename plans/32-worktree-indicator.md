# 32. Worktree indicator: icon, tooltip, info modal

User request (2026-11-11): the worktree indicator should be an icon. Hovering it shows a tooltip ("Worktree
session"). Clicking it opens a small modal that says this session is in a worktree and shows the worktree path,
with a copy button. Both clients in the same change; desktop/web only (mobile shows no location strip).

## Progress checkpoint (2026-11-11)

Both clients implement the shared icon/tooltip/modal, full copyable worktree path, branch and main-checkout path.
The focused TUI scenarios and screenshots pass, including long-path scrolling. NSH's basic tooltip/modal/copy test
passes and its screenshots are captured. The NSH long-path scenario currently fails its scrolling verification despite
showing the full soft-wrapped path in the modal; fix or refine scrolling and rerun. Paired screenshot review remains open.

## Before this change

- The location strip above the composer shows path, branch, kernel/shell counts and, for a linked worktree, a
  `⑂` glyph (ASCII `*`).
  - TUI: `nexus-tui/src/render.rs` ~7424–7480 (`linked = worktree["main"] == false`, icon `⑂`). Not clickable, no tooltip.
  - NSH: `nexus-nsh/src/ui/composer.tsx` ~740–826; `location-worktree` is a plain `Text` `⑂` with
    `accessibilityLabel="Linked worktree"`. Not clickable, no tooltip. The branch button next to it opens
    `session.worktrees`.

## Plan

- [x] **Icon:** a proper worktree icon. NSH uses a bundled SVG (`branch`-like fork glyph, theme-tinted, in
  `src/icons`). The TUI keeps a single-cell glyph from `interface/theme.toml` (`[glyphs.control]` entry `worktree`,
  unicode + ASCII) so both read from the shared theme. Same muted colour; primary on hover.
- [x] **Tooltip:** `Worktree session` on hover (NSH `tooltip`; the TUI's existing hover-hint mechanism, as for other
  strip items). Accessibility label: "Worktree session, open details".
- [~] **Click → modal** (new manifest command `session.worktree_info`, title "Worktree", placement `composer`;
  `when = ["has_session", "session.linked_worktree"]`):
  - Title: `Worktree`.
  - Body: "This session is in a worktree." then the **worktree path** in mono, wrapped (never truncated), with a
    **Copy** button (NSH clipboard; TUI OSC 52, with a "Copied" toast in both). Also the branch, and the main
    checkout's path for context (user confirmed 2026-11-11: include both, one line each).
  - Footer: `esc close` (shared modal footer). No "Manage worktrees" link (user confirmed 2026-11-11).
  - Small modal size in both clients; translucent backdrop as all modals.
- [x] Data comes from what the strip already has (`worktreeForDirectory` / the session's worktree listing); no new
  Core API.
- [~] Shared scenario with a linked-worktree session; TUI behaviour test (click the glyph opens the modal, copy emits
  OSC 52) and NSH browser test (hover tooltip, click, copy). Matching screenshots.
- [x] Docs: both READMEs, `interface/design-language.md` (location strip), `interface/capability-matrix.md`.
