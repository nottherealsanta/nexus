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
