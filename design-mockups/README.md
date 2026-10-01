# Nexus TUI design mock-ups

Static, non-functional Textual mock-ups of `nexus chat`, made to explore UI
directions. Nothing here talks to the daemon or imports `nexus`. This folder is a
standalone uv project: it is not part of the `nexus` package, its wheel, its test
suite, or its workspace, and deleting the folder removes it completely.

Every mock-up renders the **same fake session** (`fixture.py`: a 4-turn token-refresh
fix with reads, greps, a diff, a failing then passing pytest, parallel subagents, a
policy denial, a rate-limit retry, a permission prompt, a question, and dictation), so
any difference you see comes from the design, not the data.

## Run

```sh
cd design-mockups
uv run design                 # list designs, elements and keys
uv run design one             # open design 1 (also: `design 1`, `design baseline`)
uv run design four --light --state recording
uv run design elements        # gallery: every variant of every element
uv run design elements recording
uv run design mix             # your picks combined into one design
uv run design shoot           # SVG screenshots → shots/index.html
```

From the repo root: `uv run --project design-mockups design one`.

## How it is built: elements × variants → designs

The UI is split into **26 elements**, each with **3–6 lettered variants (109 in all)**:

| Element | Variants |
| --- | --- |
| `topbar` | A Today · B Breadcrumb · C Status-rich · D Minimal · E Signal tags |
| `sessions` | A Today · B Glyph column · C Table · D Two-line cards |
| `details` | A Tabs + sections · B Stacked panes · C Swatch legend · D One-liners |
| `logs` | A Plain lines · B Table + filters · C Problems first |
| `activity` | A Moving bar · B Spinner line · C Step list · D Braille pulse |
| `context-header` | A Chips · B Ledger table · C One line · D Cards · E Tree · F Stacked bar |
| `system-prompt` | A Literal text · B Sections · C Preview + stats · D Gutter + highlight · E Token outline |
| `tools` | A Grouped columns · B Group table · C Tags · D Signatures · E Permission matrix |
| `skills-mcp` | A Chip + counts · B Health list · C Toggle grid |
| `context-meter` | A Thin bar · B Stacked + % · C Numbers only · D Breakdown · E Gauge + threshold |
| `user-message` | A Box + chevron · B Prompt prefix · C Signal rule · D Quote bar · E Right meta |
| `thought` | A Muted line · B Folded · C Quote block · D Headline |
| `assistant` | A Plain Markdown · B Agent gutter · C Left rule · D Reading column |
| `reply-footer` | A Today · B Right-aligned stats · C Chips · D Rule with stats |
| `error` | A Red line · B Card + actions · C Tinted band · D Gutter mark |
| `empty` | A Hints · B Wordmark · C Start cards |
| `tool-call` | A Arrow line · B Bullet + result · C Labelled card · D Ledger row · E Signal tag · F Sentence |
| `diff` | A Split · B Unified · C Stat only · D Changes only |
| `tool-details` | A Labelled sections · B Two columns · C Terminal replay |
| `subagent` | A Link row · B Card · C Tree · D Lanes |
| `permission` | A Docked list · B One line · C Signal banner · D Full detail |
| `question` | A List picker · B Numbered · C Option cards |
| `composer` | A Box + agent row · B Prompt line · C Hint footer · D Signal frame · E Status line |
| `recording` | A Composer dot · B Waveform strip · C Pill · D Signal banner · E Live transcript · F Limit meter |
| `command-palette` | A Filter + list · B Grouped · C Fuzzy matches |
| `model-picker` | A Grouped list · B Comparison table · C List + detail |

Variant **A is always today's look** where one exists. Each element is drawn on the
same samples in every variant: for example, recording is shown in all five phases
(first-use consent, listening, transcribing, inserted, error), and tool calls as
running, ok, failed, and long-output-clipped.

A **design** is a layout + a palette + one pick per element:

| # | Design | Idea | Layout · palette |
| --- | --- | --- | --- |
| 1 | baseline | Today's TUI rebuilt statically; the control | classic · nexus |
| 2 | focus | One centred column, sidebars on demand, one-line rows | focus · nexus |
| 3 | workbench | IDE/lazygit: titled boxes, stacked right panes | workbench · workbench |
| 4 | ledger | Context first: tokens per row, meter pinned on top | ledger · ledger |
| 5 | inspector | Compact timeline + full detail of the selected row | inspector · nexus |
| 6 | signal | The web's Signal language in the terminal | classic · signal |
| 7 | paper | Light reading mode, no borders, tool calls as sentences | paper · paper |

Layouts: `classic`, `focus`, `workbench`, `ledger`, `inspector`, `paper`.
Palettes (each dark + light): `nexus`, `signal`, `ledger`, `workbench`, `paper`.

## Pick and combine

1. `uv run design elements`: ↑/↓ choose an element, **a–f** pick the variant you like
   (★ marks it), `x` clears, `p` cycles the palette, `t` dark/light. The "used by" line shows
   which designs use each variant.
2. Press **m** (or run `uv run design mix`) to see your picks combined. In the mix,
   **L** cycles the layout and **c** the palette; both are saved.
3. Picks live in `picks.json` here (git-ignored). `uv run design picks` prints them;
   `--reset` clears them. You can also pick from the command line:
   `uv run design mix --layout focus --palette signal --pick recording=c --pick tool-call=d`.

## Keys in a design

| Key | Does |
| --- | --- |
| `1` … `0` | state: idle · streaming · permission · question · recording · empty · palette · model · tool · narrow |
| `t` / `c` | dark ↔ light / cycle palette |
| `[` / `]` / `l` | sessions sidebar / details sidebar / logs drawer |
| `n` / `p` | next / previous design |
| `g` | element gallery |
| `L` | cycle layout (mix only) |
| `?` | the design's idea, its picks, and these keys |
| `q` | quit |

The last row of every screen (`design 3/7 · …`) belongs to the viewer, not the design.

## Screenshots

`uv run design shoot` writes `shots/<nn-design>/<key>-<state>-<dark|light>-<WxH>.svg` for
every design × state × palette, one sheet per element in `shots/elements/`, and
`shots/index.html` to browse them all. Options: `design shoot two ledger mix`,
`--sizes 140x42,80x24`, `--dark`/`--light`, `--palette signal`, `--no-elements`.

For live CSS-free tweaking, edit a variant in `src/design_mockups/elements/*.py` and
reopen it with `uv run design elements <slug>`.

## Files

| File | Owns |
| --- | --- |
| `fixture.py` | the one fake session every mock-up renders |
| `kit.py` | palettes (role names match `nexus/ui/tui/theme.py`), `Ctx`, small drawing helpers |
| `elements/` | the element registry (`__init__.py`) and the variants, by area |
| `designs.py` | layouts and the seven named designs |
| `app.py` | the Textual viewer, gallery and overlays |
| `picks.py` | `picks.json` load/save |
| `shoot.py` | screenshots, element sheets and `index.html` |
| `cli.py` | the `design` command |

## Tests

```sh
uv run pytest -q    # every variant in every palette; every design in every state at 140×42 and 80×24; pick → mix
```
