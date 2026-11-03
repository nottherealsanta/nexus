# nexus-mermaid

Shared Mermaid rendering for the TUI and NSH. One crate, three layers:

- `text` — the shared pie, Gantt and timeline rows. Pure Rust with only
  `unicode-width`. This is the default feature and keeps a text-only caller on
  Rust 1.85.
- `svg` — Mermaid to SVG through [Ondine](https://github.com/wevm/ondine).
  Ondine's `compiler` crate is unpublished, so `Cargo.toml` pins the upstream
  revision. It requires Rust 1.97 and edition 2024; enabling this feature raises
  the caller's toolchain. Links and images are disabled, source, output and edge
  counts are bounded, and a compiler panic becomes an error.
- `raster` — the same SVG as a PNG, drawn with the bundled Monaspace Argon so
  the browser, macOS and iOS produce the same picture. Native SVG renderers do
  not all resolve CSS variables or draw text, so this is the shared artifact for
  clients that draw an image. It has its own pixel, side and PNG byte limits and
  refuses a diagram that would shrink below the legibility floor.

Both clients use all three layers: pie/Gantt/timeline as text rows, every other
supported diagram as the shared PNG, and the source as the fallback. The TUI
paints the PNG through `ratatui-image` (terminal image protocol, or half blocks);
NSH draws it with `Image`.

```sh
cargo test                      # text only, Rust 1.85+
cargo test --all-features       # needs Rust 1.97+
cargo clippy --all-features --all-targets
```

A compiler panic is returned as an error (`Error::Panicked` / `RasterError::Panicked`);
a wasm build that sets `panic = "abort"` cannot unwind, so the source, output and
pixel limits are its first line of defence. No external image or link is resolved.

`svg` and `raster` remain opt-in. `nexus-tui` enables `raster` and therefore
declares Rust 1.97; NSH's native crate enables both on every target and exposes
`render`, `renderText` and `renderPng`. A theme is validated before it is
substituted (hex or keyword colours, a font-family list, lengths and a weight),
values are escaped at both insertion points (including the root `font-family`
attribute and the `color-mix` fallback), and the themed SVG is checked again;
see `resolve_theme`.

The Grok Build attribution for the TUI's box-art renderer stays in
`../nexus-tui/NOTICE`; Ondine and Monaspace are recorded in `NOTICE`.
