# Main consolidation — 2026-11-08

The user requested merging the current work onto main, updating PLAN.md and stopping.
This is a work-in-progress consolidation, not a release-ready or completed-plan claim.

## Validation

- Core full offline suite in an isolated home: **1255 passed, 1 skipped, 1 failed** in 166 s. The terminal shutdown test `test_name_persists_and_legacy_metadata_defaults` raised `PermissionError` while signalling its process group; an immediate isolated rerun passed (0.09 s). This load-sensitive failure remains recorded, not declared fixed.
- TUI full suite with loopback fixture access: **825 passed, 15 failed, 9 ignored**. Three stale expectations were corrected (Git navigation tab, removed editor.edit command, SSE invalidation instead of periodic inspector polling). The subsequent behaviour/inspector suites passed (77 + 9 tests), as did the focused manifest test. The 12 shell/editor checks below remain failing and need triage against the new editable mode; they are not all assumed to be test-only issues.
- TUI `cargo check`, `cargo check --tests`, formatting and Clippy with warnings denied passed during consolidation.
- NSH `nsh check` passed. The editor/Git browser suite reached **17/18**; its remaining test used a non-pressable Editor node to restore focus. Closing Find now returns focus to the editor, and the case-sensitive search/replace-all/undo test passes in isolation after that fix. The full suite was not repeated after it.
- Shared checks: **208 commands**, **602 contrast/theme checks**, Core Ruff clean.
- Native Rust service tests: **8 passed**, including explicit-home discovery isolation and bootstrap bounds. GPUI host compiled and its bundle-capability regression test passed earlier in the session; no native window/audio/device runtime verification is claimed.
- Earlier focused Git backend/API, real PTY, watcher, Files mutation, Manager fixture, host proxy, usage and mobile-rule results are recorded in PLAN.md and associated docs. They do not replace final broad client coverage.

## Remaining failing TUI shell checks

- `an_outside_change_reloads_a_clean_editor_and_warns_a_dirty_one`
- `disk_file_reads_follow_clean_edits_and_preserve_dirty_edits`
- `esc_leaves_a_clean_editor_and_asks_before_dropping_changes`
- `file_view_search_filters_jumps_and_ends_before_closing`
- `file_view_highlights_matches_and_announces_the_line_range`
- `long_lines_wrap_in_the_file_view_and_its_end_stays_reachable`
- `long_lines_wrap_while_editing_and_the_cursor_follows`
- `shell_anatomy_at_every_size_dark_and_mono`
- `the_editor_saves_with_the_revision_it_read_and_follows_the_new_one`
- `the_file_view_is_a_modal_at_every_width`
- `the_file_view_has_a_header_a_gutter_and_a_search_row`
- `markdown_files_open_as_prose_and_keep_the_source_behind_p`

## Remaining gates

Paired Git safety/grouping/navigation and screenshots; complete TUI vendored-editor pointer/paste/find/replace and shared-scenario coverage; final full client suites; release PR conflict resolution and installation matrix; native audio, simulator/device and GPUI runtime checks; and all other unchecked/partial PLAN.md items remain open. Deferred ZMX/browser/CI work remains deferred.

No real-provider spend, real-home migration/deployment, unique-history branch pruning or release publication occurred. Existing interface/.DS_Store metadata was preserved outside the source commit.
