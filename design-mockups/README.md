# Nexus Ratatui design mock-ups

Static, keyboard-operable mock-ups of the **native Ratatui client** (`nexus chat`),
built from the real component kit in [`rust/widgets`](../rust/widgets). They show
every screen and state from one fake world (`src/fixture.rs`) and never talk to the
daemon or import `nexus`. This folder is standalone and deletable: removing it
removes no product code (`rust/widgets` stays).

The spec is [`plans/RATATUI_DESIGN_MOCKUPS_PLAN.md`](../plans/RATATUI_DESIGN_MOCKUPS_PLAN.md).

## Run

```sh
cd design-mockups
cargo run                          # interactive viewer (F1 for keys)
cargo run -- settings-models       # open on a screen
cargo run -- list                  # screens and their states
cargo run -- screen settings-providers --state 1 --theme light --size 80x24   # print as text
cargo run -- shoot                 # shots/<screen>/<state>-<theme>-<WxH>.{txt,svg} + shots/index.html
cargo test                         # every screen × state × theme × size, plus keyboard journeys
```

`shoot` options: `--screen KEY`, `--theme dark|light|mono`, `--size 80x24|120x36|200x50`, `--out DIR`.
SVGs open in any browser (serve `shots/` over HTTP if your browser blocks `file:`).

## Viewer keys (the last row belongs to the viewer, not the design)

| Key | Does |
| --- | --- |
| `F2` / `Alt+Shift+Tab` | next / previous screen |
| `F3` | next state of this screen (resets the fake world) |
| `F4` | dark → light → mono |
| `F5` | size: fit → 80×24 → 120×36 → 200×50 |
| `F7` | Unicode ↔ ASCII glyphs |
| `F8` | show focus id and counts |
| `Ctrl+Q` | quit |

Everything else goes to the screen, so it behaves as the plan specifies:
`↑↓` move, `←→` value/pane, `Tab` next stop, `Space`/`Enter` toggle/activate,
`/` search, `Alt+↑↓` reorder, `Delete` remove, `Ctrl+PgUp/PgDn` tabs, `Esc` ladder,
`Ctrl+X X` dismiss toasts, `Ctrl+X N` notifications, `Ctrl+X A` undo. Changes act on
an in-memory copy and raise a `Saved … (mock)` toast. The mouse works (click, `×`).

## Files

| File | Owns |
| --- | --- |
| `fixture.rs` | the fake world (`World`) and per-screen view state (`View`) |
| `screens.rs` | the registry, chat shell, gallery, toasts and overlay screens |
| `settings.rs` | Settings frame and one page per area |
| `sessions.rs` | the one Sessions surface (docked sidebar and narrow drawer) |
| `app.rs` | viewer state, state presets, keys, mouse |
| `shoot.rs` | `Buffer` → text / SVG / `index.html` |
