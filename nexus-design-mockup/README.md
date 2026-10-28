# nexus-design-mockup (temporary)

Mock-ups for [DESIGN_PLAN.md](../DESIGN_PLAN.md). Delete this folder when the
plan has landed. Nothing in the product depends on it.

**Open `shots/index.html`** to choose a design for each decision: compare the
recommended option, the alternatives and today's TUI, add notes, then press
**Export choices** and save the file as `choices.json` in this folder.

| Path | What |
| --- | --- |
| `theme.toml` | The proposed theme (`interface/theme.toml` plus the D01 additions) |
| `tui/` | A standalone Ratatui crate that draws every TUI mock-up into a buffer, then writes SVG and text (`cargo run -- list`, `screen <id> [dark\|light\|mono] [--ascii]`, `shoot`, `css`) |
| `tui/src/shell.rs` | The proposed shell (header band, sidebars, composer) and timeline helpers |
| `tui/src/screens/dNN_*.rs` | One module per decision. `alt.rs` holds the alternative options |
| `nsh/` | Static HTML/CSS mock-ups of NSH. `tokens.css` is generated from `theme.toml` |
| `before/` | Captures of the TUI as it is today (real binary, real Core) |
| `reference/` | Old Nexus screenshots and main-branch mock-ups |
| `options.json` | The chooser's content: decisions, options and their shots |
| `shoot.sh` | Renders everything: `./shoot.sh [all\|tui\|nsh\|gallery] [id-prefix]` |
| `crop.sh` | Zooms into a region of a shot for review |

Rendering uses Playwright's cached `chrome-headless-shell`, not node (Homebrew
node is broken on this machine). TUI shots are 200×50 unless the decision is
about narrow widths. Cells are 8.4×18 px in Monaspace Argon.
