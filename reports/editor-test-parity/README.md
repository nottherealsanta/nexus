# Editor test cleanup captures — 2026-11-08

TUI captures use the real renderer and application key handling against isolated in-memory fixtures, not a live/personal Core. No provider calls.

Regenerate:

```sh
cd nexus-tui
NEXUS_EDITOR_SHOTS=../reports/editor-test-parity cargo test --test shell editor_parity_shots -- --ignored
```

SVGs are converted with headless Chromium at 1009×649 (120×36 cells). Source, Find and Markdown preview PNGs were opened and inspected.

Shared fixture: `hello.md` contains:

```markdown
# Editor parity

needle Needle needle
End inserts after the last character.
```

- `tui-editor-source`: directly editable source, line numbers, Markdown syntax, preview shortcut in header.
- `tui-editor-find`: query `needle`, case-sensitive on, replacement `thread`, second case-sensitive match selected through Enter twice. Both lowercase occurrences are visibly highlighted; `Needle` is not. The Find row reports two matches. The TUI does not visually distinguish the active match from the other marked occurrence.
- `tui-editor-preview`: after case-sensitive replace-all, closing Find and undoing both replacements, Ctrl+Shift+P enters preview. The fixture test asserts original source is restored before preview.

NSH counterpart capture/comparison is pending in the parent task. These captures alone do not establish complete parity.

**Existing unresolved parity gap:** NSH's Editor enables `softWrap`; the vendored TUI editor horizontally scrolls. The vendored crate has no soft-wrap configuration/API found in its editor/view/render implementation. Implementing wrapping requires coordinated visual-row, cursor, pointer and scroll mapping changes, not merely toggling an option. Long-line reachability tests pass but do not assert wrapping. Keep this gap open in PLAN; it is not an approved client-specific difference.
