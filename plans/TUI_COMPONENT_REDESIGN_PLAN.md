# Native TUI component redesign and reliability

Status: main behaviors implemented and tested; remaining component migration is
incomplete. Unchecked items below must not be described as complete.

## Contract and order

Preserve labelled context and the host boundary. Do not delete sessions on tab close,
repeat side-effecting tools on ambiguous failures, or lose composer text. Native TUI
only; no new web/desktop redesign. Each stage needs focused tests and matching docs.

1. Reliability baseline: trace tool completion to next model request and UI update;
   reproduce tool/subagent/provider failures and reconnects using scripted providers.
   Fix verified causes; otherwise report uncertainty and add bounded diagnostics.
2. Input: Escape finishes dictation and inserts without sending, even with auto-send;
   Enter sends. Keep explicit discard. Word-aware wrapping shares source offsets with
   rendering/cursor/navigation; oversized tokens may split at grapheme boundaries.
   `/close` shares existing tab-close policy without deleting sessions/cancelling turns.
3. Foundation: shared panel, section, button, selectable row, toggle, badge, inventory
   grid and key/value geometry. Theme roles distinguish text, quiet metadata, focus,
   hover, borders and status. Orchestrator orange; build blue; custom overrides remain.
4. Context: compact/expanded/inspection/loading/error use the same sections. Tools
   use 3/4/5 columns by available width (fewer in narrow terminals), up to five rows;
   footer `[N tools]` and omitted count. Skills use TWO columns and FIVE rows, each
   with a labelled token estimate; unavailable estimates must not be invented. MCP
   shows up to five servers and status. Counts and full inspection remain available.
5. Chrome: workspace/branch/worktree align with composer editable text, abbreviate
   true home descendants to `~`, retain full paths in details. Composer right-side
   context uses quiet grey. Sidebar uses structured session/changes sections,
   readable file paths/counts and preserved diff expansion and scroll behavior.
6. Models: one row/name per model, provider discriminator and favorite marker;
   Ctrl+I (after binding check) opens full identifier/capability/limits/pricing details
   without selecting a model or losing search/selection.
7. Markdown: reuse https://github.com/celestia-island/ratatui-markdown after license
   and dependency review. Retain attribution. Support upstream CommonMark/GFM
   features for agent replies, including headings, emphasis, lists, quotes, links,
   code, tables and task lists where supported. Terminal-safe image/HTML/diagram
   fallbacks must be explicit; no silent content loss. Preserve streaming, copy
   source, wrapping, selection and cached transcript performance.
8. Component migration and verification: use components across native controls;
   test narrow/medium/wide terminal sizes, both sidebars, long Unicode text, large
   inventories, duplicate model names and error/empty states. Run Python/Rust tests,
   existing PTY checks and native screenshot review; update docs/module-map.md for
   new modules and relevant subsystem docs.

## Hover transitions

Mouse movement changes component emphasis only, never geometry. Interpolate hover
background/border over approximately 100ms with a bounded redraw rate, only while
transitioning. Selected/keyboard-focused/disabled states remain distinguishable.
Use terminal-safe stepped colors in reduced-color environments; no flashing or
pulsing. Reuse hit rectangles for rendering and mouse actions. Hover must not
select rows, change keyboard focus, send host commands or leave stale highlights
when overlays/tabs change. Verify MouseMoved delivery and current event loop before
choosing a timer implementation.

## Completion checklist

- [x] post-tool continuation regressions; error-stop tools fixed, original stall unverified
- [x] voice Escape non-destructive and never auto-sends
- [x] word wrapping integrated with render/navigation
- [x] /close shares native close policy
- [x] shared components and bounded hover transitions
- [x] context inventories; tools grid, skills 2x5 with tokens, MCP status
- [x] colors, composer metadata and workspace alignment (build `#5C9CF5`)
- [x] right sidebar redesign
- [x] single-line model selector with details
- [x] licensed upstream Markdown with streaming regressions
- [ ] remaining component migration
- [x] native/Python verification and documentation

## Verification and remaining work

Full offline Python suite: 5,160 passed, 312 skipped, 4 deselected before the final
completion/prompt/nav hover wiring and requested blue update. Focused tests after
that wiring: 93 passed. Rust: 101 passed, 2 ignored. Playwright native terminal
capture/check rerun passed after final wiring at wide/narrow widths, including
model picker and Markdown captures. `git diff --check` passed.

Hover now covers composer controls, menu/model rows and toggles, top/details tabs,
settings navigation, prompt choices and completions. Remaining migration includes
form buttons, session rows, agent/detail actions and shared section primitives.
Original post-tool stall remains unreproduced. Verified fix: explicit provider
error stop with collected tools fails before executing those tools. Bare EOF is
still treated as end_turn by current provider/core contracts; no EOF fix claimed.
