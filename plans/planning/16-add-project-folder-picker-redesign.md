# Plan 16: A better-looking "Add project" folder picker (Ratatui)

Status: implemented and offline/PTY verified; pixel before/after review remains open (2026-10-26). Source: root `TODO.md` item 9,
quoted unchanged:

> - ![alt text](image.png) better way to render this

## 1. What the image shows

`image.png` (repo root, read 2026-10-25) is a screenshot of the native **Add project** modal: a
bordered box titled ` Add project · Esc cancel `, a highlighted first row
`› Path: /Users/santa/repos`, then `Open this folder · new session`, `.. /`, and a flat
alphabetical list of folder names each ending in ` /` (`ai-engineering /`, `alpha-toe /`, … `mars /`).
There is no key hint, no count, no scroll indicator, no visual difference between the path field,
the action and the directory rows, and every row has the same weight.

Interpretation (assumption A1): "better way to render this" asks for a clearer visual design of
the same picker, not new filesystem powers.

## 2. Current state (checked 2026-10-25)

- Drawing: `Sidebar::draw_projects` in `rust/tui/src/sidebar.rs:970-1061`. In picker mode it clears
  the selector area, draws `Block::bordered().title(" Add project · Esc cancel ")`, and builds plain
  labels: `Path: …` (with a `▏` cursor while editing), `Open this folder · new session`, then one
  row per entry as `name /` or `name · file (not selectable)`; errors in red, truncation as
  `… more entries not shown`.
- Data: Python `Sidebar.browse_project` (`nexus/ui/ratatui/sidebar.py:135-151`) asks the host
  `ProjectDirectoryList` (`nexus/host/protocol.py:1075-1082`) and adds a `..` row. Entries are
  `{name, path, kind: dir|file|link, size}`, directories first, hidden entries omitted
  (`protocol.py:2343-2348`), bounded (limit 500).
- Behaviour (keep): `docs/ratatui-parity.md:2492-2523` — editable path (`/` or click), Enter on a
  directory opens it as a project, Right drills in, Left/Backspace goes up, files inert, Escape
  cancels, no filesystem access in the client.

## 3. Design

Keep every behaviour; change only presentation, plus one optional host field (§3.4).

### 3.1 Structure

```
╭─ Add project ─────────────────────────────────────────────── esc ╮
│  ~/repos                                              23 folders │  breadcrumb (path), count
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ / filter or type a path…                                      ││  input (editing state)
│  └──────────────────────────────────────────────────────────────┘│
│  ↵ Open ~/repos as a project                                     │  primary action, accent
│ ──────────────────────────────────────────────────────────────── │
│  ▸ ..                                                            │
│  ▸ ai-engineering                                         git    │  dir rows: glyph + name (+ marker)
│  ▸ alpha-toe                                                     │
│  …                                                         ▐█▌   │  scrollbar when it overflows
│ ──────────────────────────────────────────────────────────────── │
│  ↑↓ move · ↵ open · → look inside · ← up · / type path · esc     │  key hints, muted
╰──────────────────────────────────────────────────────────────────╯
```

- **Header**: the current directory as a breadcrumb, `~`-shortened for display only (the
  editable value stays absolute), with the row count right-aligned (`23 folders · 4 files`,
  `+ more not shown` when `truncated`).
- **Input row**: a distinct field (panel background, border or underline) instead of a list row.
  While editing it shows the shared editor cursor. When not editing, typing a printable character
  filters the visible rows (client-side, over the rows already listed; Q2) — this is new behaviour
  and can be dropped if unwanted.
- **Primary action**: `↵ Open <dir> as a project` in the accent colour, its own row, separated by
  a rule from the list. Enter with the list focused still opens the *selected* row (unchanged).
- **Rows**: a directory glyph (`▸`, or `>` under `NEXUS_ASCII=1`) and the name, no trailing ` /`.
  Files are dimmed with no glyph and no "not selectable" text (the dim colour says it; the
  details stay on hover via a muted suffix `file`). Links show `↗`.
- **Selection**: full-width `p.element` background (existing selection style) instead of the `› `
  marker only.
- **Scrollbar** on the right edge when rows overflow (the selector list already scrolls,
  `paint_list`).
- **Footer**: one muted key-hint row (the Projects list already has one, `sidebar.rs:921`).
- **Errors**: a red line directly under the input, not at the bottom of the list.

### 3.2 Geometry and hit-testing

One layout function returns the rects for header, input, action, list and footer; drawing and
mouse hit-testing both use it (the `top_layout`/`tab_cells` pattern in `render/chrome.rs`). Mouse:
click input → edit; click action → open current; click row → open that directory (unchanged);
wheel scrolls the list.

### 3.3 Narrow terminals

Below ~40 columns: drop the count and the right-hand markers, then the footer; the input and the
list always stay.

### 3.4 Optional host field (Q1)

A `git` marker needs to know whether a folder is a Git work tree. That is a host fact
(`ProjectDirectoryList`), not something the client may probe. If wanted, add a bounded boolean
`repo` per directory entry (a single `stat` of `<dir>/.git`, no Git process) to the result, with a
test in `tests/test_host_workspace_browse.py`. Without it, omit the marker.

## 4. Implementation steps

1. Rust: split `draw_projects` picker branch into a `draw_project_picker` with the layout function;
   new hit-test using the same layout; keep the existing actions (`browse_project`, `project_path`,
   `select_project`, `cancel_project`) unchanged.
2. Optional client filter (Q2).
3. Optional host `repo` flag (Q1).
4. Docs: `docs/ratatui-parity.md` "Project directory picker" section describes the new look;
   "not visually verified" until a capture is reviewed.

## 5. Tests and acceptance

- Rust `sidebar.rs` tests: header shows the path and counts; action row distinct from rows; files
  dimmed and inert; selection highlight; scrollbar only on overflow; footer hints; narrow-width
  degradation; ASCII mode; every click target maps to the drawn row.
- PTY (`tests/test_ratatui_pty_sidebar.py`): open Add project, navigate, open a folder; a native
  capture compared by eye with `image.png` (before/after).
- Acceptance:
  - [ ] Same behaviour as documented in `docs/ratatui-parity.md:2492-2523`.
  - [ ] Path field, primary action and list are visually distinct; hints and counts visible.
  - [ ] No client filesystem access is added.

## 6. Open questions

- Q1. Show a `git` marker (needs the host field)?
- Q2. Type-to-filter in the list (vs. typing always editing the path)?
- Q3. Should the modal be larger than the selector area (`sidebar::selector_area`) for long paths?

### Native picker fixture follow-up

The native sidebar PTY fixtures now explicitly enter `browser_open` mode, assert
the redesigned `▸ src` directory presentation and `Open /nexus as a project`
action, and exercise bracketed-paste path editing inside the picker. Combined
sidebar / Manager-show / picker PTY run: 101 passed. Before/after visual capture
review remains unverified; this plan is not archived on test results alone.

### Finisher visual and boundary audit

Both existing picker PTY tests passed in the focused run. Human before/after screenshot
review was NOT performed; PTY textual capture assertions are not visual approval.
No filesystem enumeration was added to Rust picker/sidebar code. Repository-wide Rust
filesystem uses include pre-existing TTY, configuration and diagnostic logging paths;
“no client filesystem access” here means no project-folder/session enumeration bypassing
the host, not an assertion that the whole native process never opens a file.

### Picker/native continuation

All six native sidebar/picker tests pass in the 112-test focused run, including
path editing/paste and directory presentation. Corrected the separate session
strip PTY ordering to the documented basename → branch → full-path contract
(docs/ratatui-parity.md, project strip). Capture helper now explicitly signals
SIGWINCH to the native process group and waits for output, rather than assuming
the setup wrapper's foreground TTY group propagates resize. No picker behavior
or filesystem boundary changed. Screenshot comparison remains unverified.

Native captures at 40/140 columns saved under ignored
`artifacts/planning-acceptance/picker-*`; directory row asserted in each. Inspected
terminal output contains path, primary action and directory section. ANSI-stripped
text loses cursor-position spacing, so it is not reliable evidence of final border
alignment; full pixel before/after review remains genuinely outstanding.
