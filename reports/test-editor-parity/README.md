# Same-state editor comparison — 2026-11-08

Both clients show `AGENTS.md` with `# Title\n\nSome **bold** text.\n`. Source is directly editable. Find state has query `bold`, default case-insensitive matching, an empty replacement and one highlighted occurrence. No replacement has been performed.

The four PNGs were opened and compared. Both source captures retain Markdown syntax and line numbering; both Find captures show the same match and replacement controls. These establish matching content/state, **not complete visual parity**:

- NSH puts Find/Replace above the source; TUI places it at the panel bottom.
- NSH exposes Preview/Diff/Reload/Save header controls; TUI header shows Back, editing state and preview shortcut.
- NSH status says `saved`; TUI says `editing`.
- The images are different viewport sizes (NSH 1280×900, TUI 120×36 cells / 1009×649).
- The separate known long-line gap remains: NSH soft-wraps, TUI horizontally scrolls. This short fixture does not test wrapping.

No new client-specific difference is approved by these captures. Existing parity gaps remain open.

TUI uses the real application fixture/renderer. Regenerate both the richer fixture and matching NSH fixture:

```sh
cd nexus-tui
NEXUS_EDITOR_SHOTS=../reports/editor-test-parity NEXUS_EDITOR_SHARED_SHOTS=../reports/test-editor-parity cargo test --test shell editor_parity_shots -- --ignored
```

Convert TUI SVGs with Chromium at 1009×649. The richer case-sensitive replace-all/undo/preview fixture remains separately in `reports/editor-test-parity/`.
