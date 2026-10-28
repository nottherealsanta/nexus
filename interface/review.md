# TUI visual review

These captures show the real TUI in a PTY against an isolated Core, using
`nexus-tui/tools/webterm`; they are review evidence, not a release sign-off.
The reference implementation is `../nexus/rust/tui/src`.

| Capture | Reviewed result |
| --- | --- |
| [Dark shell, 120×36](review/session-120x36-dark.png) | Session rows, activity groups, agent name and quiet non-Git state are legible; scope/archive controls fit. |
| [Light shell, 80×24](review/session-80x24-light.png) | Narrow timeline, Mermaid source and composer fit; earlier context is reachable by scrolling. |
| [Expanded activity](review/tools-expanded-120x36-dark.png) | Group header and members are distinguishable, with a visible focus bar. This captures expanded rows, not every tool detail. |
| [Dark Settings](review/settings-120x36-dark.png) | Framed centered panel and searchable root list have consistent surfaces. |
| [Light Settings, 80×24](review/settings-80x24-light.png) | Narrow panel fits; an overflow notice now announces additional sections. |
| [Confirmation](review/confirm-delete-120x36-dark.png) | Framed confirmation, safe default and destructive action are distinct. |
| [Real worktree groups](review/worktrees-120x36-dark.png) | The main checkout and linked worktree are grouped and labelled with branches from Core. |
| [Composer selection](review/editor-selection-120x36-dark.png) | Shift-selection is visible inside the composer; keyboard copy and undo are tested separately. |
| [Composer and CJK draft](review/composer-multiline-120x36-dark.png) | A multi-line CJK draft, the capitalised agent name and the location line; captured after the composer increment. |
| [Files tree](review/files-tree-120x36-dark.png) | Lazy expansion, Git letters (`M`/`?`/`D`), muted ignored entries; the selected folder is open. |
| [File view](review/files-open-120x36-dark.png) | Read-only file content in the centre column with line numbers and a middle-truncated path header. |
| [Collapsed paste](review/editor-paste-80x24-light.png) | Bracketed multiline paste is represented by a compact marker; the original text is sent. |
| [Code fence](review/fence-120x36-dark.png) | A focused fence has a Copy action and remains individually selectable; the language header and code share a full-width panel. |
| [Mermaid](review/mermaid-120x36-dark.png) | Supported diagram and original source both remain visible. |
| [Settings detail](review/settings-detail-120x36-light.png) | A drill-in page uses the same framed panel and shows unavailable controls plainly. |
| [Attention](review/attention-error-120x36-dark.png) | An isolated missing-sign-in error marks the other session with a visible error glyph. |
| [Offline dark](review/reconnecting-120x36-dark.png), [offline narrow light](review/reconnecting-80x24-light.png) | Connection status and read-only composer remain visible. |

Source comparison: old `render/dialogs.rs` uses rounded frames and a centered
Settings panel capped at 120 columns; the new panel follows that geometry. Old
`settings_page/root.rs` uses a searchable root and drill-in pages; the new
Settings has that navigation, though its rows remain manifest-backed. Old
`transcript/groups.rs` groups activity and old `markdown.rs` supplies the
adapted CommonMark rendering; supported Mermaid diagrams appear above their
original source. The old grapheme editor now owns the
composer buffer, selection, undo/redo and paste markers.

A live old/new side-by-side comparison was not run: old
`rust/tui/src/trace.rs` writes stall logs directly under `$HOME/.nexus`, and
`nexus/model/effort_preferences.py` also defaults there. Both bypass the
`NEXUS_HOME` override; this workspace must not touch that directory. The old
checkout was read only. Source comparison and review of the new captures do
not establish visual equivalence.

The shell was also captured at [80×24 dark](review/session-80x24-dark.png),
[120×36 light](review/session-120x36-light.png), [160×48 dark](review/session-160x48-dark.png)
and [160×48 light](review/session-160x48-light.png), plus
[monochrome](review/session-120x36-mono.png), [ASCII](review/session-120x36-ascii.png)
and [256-colour](review/session-120x36-256.png). These frames were refreshed
after the composer increment. They show the shell and source-preserving diagram
presentation; they do not cover every interaction. The seeded fixture has no
usage events, so price-tier numbers and the hover tooltip are covered by buffer
tests rather than these captures.

Remaining differences and limits:

- A safely isolated running old client has not been compared side by side.
- The old editor's mouse-drag selection and draft history are not wired to the
  composer; keyboard selection, copy, undo/redo and collapsed paste are wired.
- The Settings root does not inline the old quick toggles. Many settings remain
  unavailable until their Core APIs exist; detail pages and all dialog variants
  still need broader interaction review.
- Sidebar unread/error state is client-local and starts when this TUI observes
  events. Branches come from one project checkout listing per project, so all
  rows load together; worktree grouping was exercised against real Git.
- Mermaid flowcharts, sequence and state diagrams use the old text renderer;
  pie, Gantt and timeline use its textual alternatives. Other diagrams and
  ASCII mode show source; image rasterization and terminal image protocols are
  not ported. Full source is always shown and can be copied.
- The composer's capitalised agent, pending model marker, price-tier
  occupancy and hover explanation are covered by buffer tests; no live capture
  of those states has been taken yet. Reasoning duration is measured from the
  stream and covered by tests; the seeded frames contain no reasoning turn.
- File contents are read-only and shown one page at a time; search, syntax
  highlighting, edits and Git diff navigation remain open. The original
  cross-client plan's queue/handoff, artifacts, NSH parity and release checks
  remain future work.
