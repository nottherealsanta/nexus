# TUI design mock-ups plan

> **Status (implemented on branch `feat/design-mockups`).** Where this plan and
> `README.md` differ, the README and the code win. Changes from the original plan:
> - The project lives in `design-mockups/` as a **standalone** uv project, not a
>   workspace member: the root `pyproject.toml` is untouched. Run it from this folder
>   (`uv run design one`) or with `uv run --project design-mockups design one`.
> - Added an **element × variant** layer: 26 elements (recording, context header,
>   system prompt, tools, …) with 3–6 variants each. Designs are picks of variants, an
>   element gallery (`design elements`) shows every variant side by side, and
>   `design mix` combines your own picks (saved in `picks.json`).
> - A tenth state, `recording` (key 5), was added; `narrow` moved to key 0.
> - Screenshots go to `design-mockups/shots/`, not `artifacts/`.

Goal: a set of **static, non-functional Textual mock-ups** of the `nexus chat`
interface. Each one opens with a single command (`uv run design one`) and shows
a complete, realistic screen so we can compare layouts and finishes side by
side before changing the real TUI.

Non-goals: no daemon, no host client, no real sessions, no input handling
beyond switching what the mock-up shows. Nothing here ships in the `nexus`
wheel, and nothing here changes `nexus/`.

---

## 1. Where it lives and how it runs

### 1.1 Separate uv workspace member (recommended)

Put the mock-ups in their own small package at the repo root, **outside
`nexus/`**:

```
designs/
  pyproject.toml            # name = "nexus-designs", depends on textual==8.2.8
  src/nexus_designs/
    __init__.py
    cli.py                  # `design` entry point
    registry.py             # name → DesignSpec
    fixture.py              # the one shared fake conversation
    chrome.py               # small shared widgets (rules, chips, bars, kbd hints)
    states.py               # State enum + key bindings shared by every design
    base.py                 # MockApp base class
    d01_baseline/           # one folder per design
      app.py
      style.tcss
    d02_focus/
    ...
```

Why outside `nexus/`:

- `tests/test_ui_layering.py` restricts Textual imports to `nexus/ui/tui/` and a
  fixed list of `ui_support` files; `tests/test_docs.py` requires every module
  under `nexus/` to have a row in `docs/module-map.md`. Throw-away mock-ups
  should trip neither.
- `[tool.setuptools.packages.find] include = ["nexus*"]` means a `nexus_designs`
  package would never ship, but a `design` script in the root
  `[project.scripts]` **would** ship and point at a missing module. A workspace
  member avoids that.

Root `pyproject.toml` additions:

```toml
[tool.uv.workspace]
members = ["designs"]

[tool.uv.sources]
nexus-designs = { workspace = true }

[dependency-groups]
dev = ["nexus-designs"]
```

`designs/pyproject.toml`:

```toml
[project]
name = "nexus-designs"
version = "0.0.0"
requires-python = ">=3.13"
dependencies = ["textual==8.2.8", "textual-diff-view==0.1.5"]

[project.scripts]
design = "nexus_designs.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.package-data]
"nexus_designs" = ["**/*.tcss"]
```

Then `uv sync` (the `dev` group is synced by default) installs the `design`
script and `uv run design one` works.

Check before committing: `uv build` at the root still produces a wheel with
only `nexus*`, and release-please ignores `designs/` (add it to
`exclude-paths` in `release-please-config.json` if it would otherwise trigger a
release).

**Fallback** if the workspace gives trouble: keep the same folder and run it as
`uv run python -m nexus_designs one` with `designs/src` on the path via a
`[tool.uv] dev-dependencies` editable install. Same code, longer command.

### 1.2 The `design` command

```
uv run design                 # list designs with one-line descriptions
uv run design one             # open design 1  (also: `design 1`, `design baseline`)
uv run design two --light     # start in the light palette
uv run design four --state permission   # open straight onto a state
uv run design gallery         # open design 1; n / p cycle through all designs
uv run design shoot           # write an SVG screenshot of every design × state
uv run design shoot three --size 160x48
```

- Accept number words (`one` … `ten`), digits, and slugs. Unknown name → print
  the list and exit 2.
- `shoot` uses Textual's `App.run_test(size=...)` + `app.save_screenshot()` to
  write `artifacts/designs/<nn>-<slug>/<state>-<dark|light>-<cols>x<rows>.svg`
  (`artifacts/` is already git-ignored). Default sizes: `120x36`, `160x48`,
  `80x24` (the narrow case matters).
- Also write `artifacts/designs/index.html`: a plain grid of every SVG with the
  design name and state as captions, so all designs can be compared in one
  browser tab.

### 1.3 Live editing

Each design's styles live in a `.tcss` file, so
`uv run textual run --dev nexus_designs.d02_focus.app:FocusApp` gives hot CSS
reload while tweaking (`textual-dev` is already in the `dev` extra). Note this
in `designs/README.md`.

---

## 2. Shared pieces (build these first)

### 2.1 One fixture conversation (`fixture.py`)

Every design renders **the same data**, so differences are design only. Plain
frozen dataclasses, no imports from `nexus`. Contents must exercise every row
type the real TUI has:

| Item | Detail to include |
| --- | --- |
| Session | title "Refactor auth token refresh", agent `Build`, model `claude-opus-5-5`, provider `anthropic`, effort `high`, status, cost, elapsed |
| Session list | ~12 sessions across Today / Yesterday / Last week, one running, one needing approval, two archived |
| Context header | system prompt (~40 lines, with `<environment>` block), 14 tools, 3 skills, 2 MCP servers, AGENTS.md entry, token counts per block |
| Context usage | 61% of 200k, split into system / tools / history / attachments |
| Turn 1 | user prompt (multi-line + an attached image chip), a thought line, `Read` ×2, `Grep` with 37 matches, assistant Markdown with a heading, list, inline code and a fenced Python block, reply footer (`BUILD · model · 4.2s · 3.1k tok`) |
| Turn 2 | user prompt, `Edit` with a 2-hunk diff, `Bash` (`pytest -q`) with a failing output tail, a retry `Edit`, `Bash` passing |
| Turn 3 | a `Task` subagent (`explore`) with its own 3 tool calls and summary; two parallel subagents, one still running |
| Turn 4 (live) | streaming assistant text cut mid-sentence, activity bar running, elapsed timer |
| Errors | one tool error (permission denied path), one provider error (rate limit, retrying in 8s) |
| Permission | pending `Bash` approval: command, cwd, reason, choices (allow once / always / deny) |
| Question | an agent `AskUser` question with 3 options |
| Details sidebar | modified files (`M src/auth.py +24 −9`, `A tests/test_refresh.py +61`), MCP server status, agents tree, background shell jobs, worktrees |
| Logs | ~30 daemon log lines with levels |

Long outputs must be long enough to need clipping, so each design has to show
how it **announces** clipping (project rule: clipping is always announced).

### 2.2 States (`states.py`)

A design is one `App`; the state picks what is shown. Same keys in every design:

| Key | State |
| --- | --- |
| `1` | `idle` — finished conversation, empty composer |
| `2` | `streaming` — turn 4 live, activity bar running |
| `3` | `permission` — approval prompt docked |
| `4` | `question` — agent question picker |
| `5` | `empty` — new session: context header only, welcome/hints |
| `6` | `palette` — command palette open over the screen |
| `7` | `model` — model picker modal |
| `8` | `tool` — tool-details modal for the failing `Bash` call |
| `9` | `narrow` — forces the narrow layout regardless of terminal size |
| `t` | toggle dark / light |
| `[` / `]` | toggle left / right sidebar (where the design has them) |
| `n` / `p` | next / previous design (gallery mode) |
| `?` | overlay listing these keys and the design's one-paragraph rationale |
| `q` | quit |

A thin footer line in every mock-up shows: `design 3/7 · ledger · state: permission · ? keys`.
Keep it visually separate (dim, last row) so it is not mistaken for part of the design.

### 2.3 `MockApp` base (`base.py`)

- Holds `state`, `theme_mode`, the fixture, and the shared bindings.
- Subclasses implement `compose_for(state)`; the base remounts on state change.
- Registers the design's dark and light `Theme` (each design defines its own
  palette dict, same shape as `nexus/ui/tui/theme.py`, so tokens stay
  comparable).
- No timers except an optional spinner/elapsed tick in `streaming`, so
  screenshots are deterministic (disable the tick under `shoot`).

### 2.4 `chrome.py`

Only genuinely shared, style-agnostic helpers: Markdown block, diff block
(wrap `textual_diff_view` once), key-hint text, a token bar renderable.
Each design should still own its look; don't let `chrome.py` become a
de-facto design.

---

## 3. The designs

Seven designs. Each one is a distinct **idea**, not a recolor. Each design's
`app.py` docstring states: the idea, what it optimizes for, what it gives up.

### 01 · `baseline` — today's Nexus, recreated statically

Control group. Top bar (`▌ title … status + ▐`), sessions sidebar (34 cols),
context header, timeline, composer with `BUILD model provider effort` row,
activity bar, details sidebar (42 cols) with tabs. Palette copied from
`nexus/ui/tui/theme.py`. Without this, comparisons are against memory.

### 02 · `focus` — single column, chrome on demand

Inspired by Claude Code / minimal REPLs. No sidebars by default; content
column capped at ~100 cols and centered. Top bar reduced to one dim line.
Sessions and details open as overlays (`[` / `]`). Tool calls are one line
each (`● Read src/auth.py · 212 lines`), expandable. Context usage lives in the
composer's bottom-right (`61% ctx`). Tests whether we can drop the sidebars
without hiding information (everything must still be one key away and
labelled).

### 03 · `workbench` — boxed panes, IDE / lazygit style

Every region is a titled box (`╭─ Sessions ─╮`, `╭─ Conversation ─╮`,
`╭─ Context ─╮`, `╭─ Files ─╮`, `╭─ Agents ─╮`), with the focused pane's
border in the accent color and numbered pane shortcuts in the titles
(`[1] Sessions`). The right column is split vertically into stacked panes
instead of tabs, so files, agents and jobs are visible at once. Dense, keyboard
first.

### 04 · `ledger` — context-first

Built around the project's core idea: the context is the product. The
timeline is a ledger: a left gutter with turn number, a role glyph, and a
right gutter with **tokens added to context per row** and running total.
A persistent context bar under the top bar shows the stacked composition
(system / tools / skills / history / attachments) with percentages; hovering
or selecting a row highlights its slice. Tool rows show every parameter as a
labelled key/value line, never JSON. Shows exactly what the model sees.

### 05 · `inspector` — master / detail

Timeline on the left (~55%), a persistent inspector on the right showing the
**selected** row in full: tool parameters, full output with line numbers,
timing, tokens, which agent ran it, permission decision. Timeline rows stay
one line each. Sessions become a dropdown in the top bar. Tests whether
"compact list + full detail" reads better than expanding rows in place.

### 06 · `signal` — the web Signal language in a terminal

Port of the original `design.md` Signal finish: flat near-black canvas,
capitalized labels joined by rules (`TOOLS ──────── 14`), solid square
swatches, tinted tags, one job per signal color, solid-fill banner for
"NEEDS APPROVAL", square status glyphs (`■ ▪ □`). Same region layout as
baseline, so it isolates the effect of the visual language alone.

### 07 · `paper` — reading mode, light first

Light-first, typographic. Wide margins, no borders at all, hierarchy by
weight, spacing and indentation only. Assistant text is the hero; tool calls
collapse into a quiet margin note per turn (`3 reads · 1 edit · tests ✓`)
that expands. Good for long reviews of what an agent did. Dark variant is a
warm sepia-dark, not black.

Optional extras if time allows (keep the numbering open):

- `08 · dense` — tuned for 80×24: no sidebars, abbreviations, status
  everything in the top line.
- `09 · cockpit` — multi-agent first: a live grid of agent cards (one per
  running subagent) above a shared timeline.

---

## 4. Per-design checklist ("mock up everything")

Every design must render all of these in its own style, in both palettes.
Put this table in `designs/README.md` and tick it per design.

- [ ] Top bar: title, status, connection, cost/tokens, sidebar toggles
- [ ] Sessions list: groups, running / needs-approval / archived markers, filter
- [ ] Context header: system prompt, tools, skills, MCP, AGENTS.md, counts
- [ ] Context usage indicator
- [ ] User message (multi-line, attachment chip, collapsed variant)
- [ ] Thought line
- [ ] Assistant Markdown (heading, list, inline code, code block)
- [ ] Tool call rows: Read, Grep, Edit (with diff), Bash (pass + fail)
- [ ] Clipped output with the clipping announced
- [ ] Tool error and provider error / retry
- [ ] Subagent task (finished, running, parallel)
- [ ] Reply footer (agent · model · time · tokens)
- [ ] Streaming text + activity indicator
- [ ] Permission prompt
- [ ] Agent question picker
- [ ] Composer: placeholder, agent/model/effort row, key hints
- [ ] Details: modified files, MCP servers, agents, jobs, worktrees
- [ ] Logs drawer
- [ ] Command palette modal
- [ ] Model picker modal
- [ ] Tool details modal
- [ ] Empty / new session
- [ ] Narrow layout (80×24)
- [ ] Light and dark palette

---

## 5. Implementation order

1. **Scaffold** `designs/` package, workspace wiring, `cli.py` with `list`, name
   resolution, and a placeholder design. Verify `uv run design` and
   `uv run design one` from a clean `uv sync`.
2. **Fixture + states + base + footer.** Unit-test that the fixture has every
   row type in §2.1 (small pytest file at `designs/tests/test_fixture.py`).
3. **01 baseline**, all states. This shakes out the base class and fixture.
4. **`shoot` + `index.html`** gallery, so every later design is reviewed by
   screenshot as it is built.
5. **02–07**, one at a time, each through the §4 checklist before the next.
6. **Gallery mode** (`n` / `p`) and the `?` rationale overlay.
7. `designs/README.md`: commands, keys, one paragraph per design, checklist.

Rough size: shared code ~600 lines; each design ~300–500 lines of Python plus
a `.tcss` file.

## 6. Verification

- `uv run design` lists all designs; `uv run design <each>` opens without
  error at 80×24, 120×36 and 160×48.
- `uv run design shoot` produces 7 designs × 9 states × 2 palettes × 3 sizes,
  no exceptions; open `artifacts/designs/index.html` and eyeball every frame.
- A smoke test (`designs/tests/test_smoke.py`) that runs each design through
  every state with `run_test` and asserts no exception and a non-empty screen.
  Keep it out of the main `tests/` suite so `pytest -q` at the root is
  unaffected (root `testpaths = ["tests"]`).
- Root checks still pass unchanged: `.venv/bin/python -m pytest -q`,
  `ruff check nexus tests`, and `uv build` produces a `nexus*`-only wheel.

## 7. After review

Once a direction is picked, the follow-up is a separate plan against
`nexus/ui/tui/` (and the web mirror, since the web app must match the TUI per
`AGENTS.md`), plus a `docs/decisions.md` entry recording which design won and
why. The `designs/` package can stay as the design sandbox for future
experiments.
