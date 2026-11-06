# NSH terminal-style UI review

- `rust-new.png`, `.svg`, `.txt`: dedicated Rust TUI at 160×48 against an isolated seeded Core.
- `web.png`, `narrow-web.png`: packaged NSH web at 1280×768 and 390×844 against that same Core, with fresh browser contexts. No provider calls or prompt submissions.
- `nsh-terminal.json`: NSH terminal backend's actual 160×48 cell snapshot from a mocked new-session test.
- `nsh-terminal.png`: that cell snapshot rendered for inspection; the mock event stream closes, so its reconnecting banner is expected. Its fixture differs from the seeded Core; it is a layout/glyph comparison, not identical context data.

Compared: shared text glyphs rather than SVG fallback labels, fixed compact reading
gutters, monospace interface, aligned search input, composer and narrow title.
Terminal rendering still uses upstream's grid/color mapping, not the web's pixel
layout. Phone navigation and touch targets are retained; a narrow web screenshot is
not proof of native iOS rendering.

Validation: six `timeline-layout.test.tsx --target tui` checks pass, including
rendered glyphs, compact gutters and the narrow header. Web packaging passes.
Web-target test mocks still return HTTP 404 for two app checks; pure layout checks
pass. The existing NSH type-check issues and seven known Rust TUI test failures
remain outside this visual-alignment change.

## Terminal parity pass (2026-11-05, after NSH 0.4.0)

`nexus nsh tui` runs the TSX app through upstream's terminal backend. The Rust
frames here are `--dump-frame` SVG→PNG captures against the seeded Core
(`rust-session-120x36.png`, `rust-new-120x36.png`, `rust-settings-120x36.png`,
`rust-palette-120x36.png`). The NSH frames are cell snapshots from
`nsh test --target tui` (`nsh-terminal-session-120x36.png`,
`nsh-terminal-new-160x48.png`, `nsh-terminal-settings-120x36.png`,
`nsh-terminal-palette-120x36.png`, `nsh-terminal-session-80x24.png`). The test
harness's mocked event stream closes, so NSH frames show the expected
`Disconnected from Core` banner and read-only composer; the fixture's session text
also differs from the seeded Core. Compare structure, glyphs and key hints, not
data.

Changed in this pass: the shared `⌕` search control glyph (the TUI now reads it from
`theme.toml` instead of hardcoding it), TUI key spellings in the terminal
(`ctrl+k`, `ctrl+,`), the manifest's `keys.tui` bound alongside `keys.nsh` there, the
sidebar footer without a decorative gear, the Settings focus bar drawn as `▌` cells, and
the palette's selected row carrying the same `▌` bar as the TUI. Remaining differences, listed in PLAN.md 8.2: upstream frames modals and draws
a timeline scroll bar; the composer keeps NSH's voice and send buttons; Settings
shows segmented value controls where the TUI prints the value.

## 11-06 pass

The 0.4.0 test-host 404 is fixed (`apiBase()` resolves the web realm's page origin, so `open`'s routes answer),
so terminal cell frames no longer carry a spurious reconnecting banner and the timeline loads. Compared again
with the Rust `--dump-frame` output at 120×36 and 80×24: the Files tab now reads as the same tree (`demo` +
`⌥ master` header, `▸ nexus-core`, then files), the archived filter uses the shared `▣`, the sidebar search
glyph/key spelling match, and the narrow header is the single flexible title. The remaining visual differences
are the three listed in PLAN.md 8.2 (composer voice/send buttons, upstream modal frames and timeline scroll bar,
Settings segmented controls) and still need the user's decision.
