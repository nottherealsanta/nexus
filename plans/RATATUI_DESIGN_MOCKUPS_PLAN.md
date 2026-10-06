# Ratatui design mock-ups, component kit and UX redesign

Status: **Phases 0–2 implemented** (old mock-ups removed; `rust/widgets` kit with 13
tests; `design-mockups/` with 25 screens and 17 tests). **Stopped at the Phase 2
review gate**: Phases 3–6 (wiring into `rust/tui` and Python) are not started. Kit
files are grouped by family (`controls`, `inputs`, `lists`, `feedback`, `scroll`)
instead of one file per component, and a `scroll` container was added. Scope is the **native Ratatui
client** (`nexus chat`, `rust/tui/` + `nexus/ui/ratatui/`). The web app (to be
deprecated) and the GPUI desktop client are out of scope and get nothing ported.

This plan replaces the Textual mock-ups in `design-mockups/` with a Ratatui
mock-up project that uses a **real, reusable component kit**: the same widgets
the mock-ups draw are later used by the real TUI. It also specifies the UX
changes asked for: one-page Settings, one Sessions surface, toasts instead of
notices, and keyboard navigation everywhere.

Related ledgers this plan builds on (read them, don't redo them):
`plans/TUI_COMPONENT_REDESIGN_PLAN.md` (the current `render/components.rs` hover
work), `plans/SETTINGS_REVAMP_PLAN.md` (Settings navigation findings, §2),
`docs/ratatui-parity.md`, `docs/surfaces.md`, `docs/decisions.md` ("The 2026-10
UI redesign").

---

## Revision 1 (after reviewing the mock-ups)

These supersede anything below that says otherwise.

1. **No square brackets around controls.** In dark and light themes buttons, toggles,
   selects, segmented controls, steppers, icon buttons, list actions, the toast
   `×` and the modal close are **filled chips** (padding plus a background); the
   ordered-list add row is plain accent text (`+ Add model…`). The **mono** theme
   has no fill, so it keeps brackets as its only affordance (`kit::br`).
2. **The composer stays as it is today.** Its controls are plain words (agent in
   the build colour, model, effort), not selects or chips; no composer redesign.
3. **Toast countdown is barely visible**: the hairline is the level colour mixed
   about 22% into the toast background (DIM in mono). It hints at time left; it is
   not something to watch.
4. **Context header redesigned, same look.** Keeps `◈` in the block colour, bold
   title, dot leader, counts, right-aligned tokens, a blank row between blocks and
   the `Context total` footer. Each block (system prompt, AGENTS.md, tools, skills,
   MCP) is one focus/click target that expands in place (Enter or click, per block):
   - Tools: grid of names, 2/3/4/5 columns by width, up to five rows, off tools
     struck through, footer `N tools · M on · K not shown`.
   - Skills: two columns, up to five rows, `~tokens` per skill.
   - MCP: up to five servers with status dot, tool count, tokens, loading mode and
     scope; failures show their message in the error colour.
   - System prompt and AGENTS.md: a one-line preview.
   - Loading shows `— tok` and `tokens unavailable` (never an invented estimate);
     error shows a callout with Retry above the counts.
   Mock-up screen: `context-header` (all expanded, compact, tools open, loading, error).

## Contents

0. [What was asked](#0-what-was-asked)
1. [Scope, non-goals and invariants](#1-scope-non-goals-and-invariants)
2. [Step 0: remove the Textual mock-ups](#2-step-0-remove-the-textual-mock-ups)
3. [Architecture: kit, mock-ups, real TUI](#3-architecture-kit-mock-ups-real-tui)
4. [Design principles](#4-design-principles)
5. [Design tokens: colour, glyphs, spacing](#5-design-tokens-colour-glyphs-spacing)
6. [Focus and keyboard model](#6-focus-and-keyboard-model)
7. [Component catalogue](#7-component-catalogue)
8. [Toasts (replacing "notice")](#8-toasts-replacing-notice)
9. [Screens](#9-screens)
10. [The mock-up project](#10-the-mock-up-project)
11. [Host contract and bridge changes](#11-host-contract-and-bridge-changes)
12. [Phased implementation and checklists](#12-phased-implementation-and-checklists)
13. [Testing](#13-testing)
14. [Docs to update](#14-docs-to-update)
15. [Decisions taken here and open questions](#15-decisions-taken-here-and-open-questions)
16. [Appendices: keymap, glyphs, colour table](#16-appendices)

---

## 0. What was asked

Verbatim intent, restated as requirements. Each one has an ID used in the
checklists in §12.

| ID | Requirement |
| --- | --- |
| R1 | Delete `design-mockups/` (the Textual mock-ups) and make it again, this time **for Ratatui**. |
| R2 | Cover **both UI and UX**: how things look *and* how they are operated. |
| R3 | Build **components**: buttons, toggles, selectors "and so on" (full list in §7). |
| R4 | **Settings is mostly one page per area**, not pages inside pages. |
| R4a | Models: the three tiers (low / medium / high) are **tabs** on one page; other model ("module") settings sit **above** the tabs on the same page. |
| R4b | Providers: **one page with one section per provider**, not a list that drills into a page per provider. |
| R5 | `/session` (and `/sessions`) and the **left sidebar open the same thing**. |
| R6 | Everything is **easily keyboard navigable**. |
| R7 | A **notice** becomes a **toast that disappears**, with an **`×`** to close it. |
| R8 | A design mock-up for **everything** (every screen, every component, every state). |
| R9 | "Feel free to try new things": proposals are marked **NEW** below and can be cut. |

---

## 1. Scope, non-goals and invariants

### In scope
- A component kit (Rust, Ratatui 0.29, the version `rust/tui` already pins).
- A standalone, deletable mock-up binary that renders every screen and every
  component state from one fake fixture, and is keyboard-operable.
- The UX specification for the real TUI (Settings, Sessions, toasts, focus).
- The bridge/host changes required to wire it into `nexus chat` (§11), phased.

### Non-goals
- No web or GPUI desktop changes.
- No new host *capabilities*: every setting shown already exists behind a host
  command. New host commands are only added where a UI needs data it cannot get
  today (each one is listed in §11 with the reason).
- Keybinding *editing* (Keyboard page is read-only in this plan).
- Mouse-first design. The mouse keeps working (click, hover, scroll), but every
  design decision is checked keyboard-first.

### Invariants (from `AGENTS.md`, restated so nobody breaks them)
1. **Context is visible.** No setting, parameter, path or error the agent can see
   is hidden from the user. Clipping is always announced (`… 12 more`).
   Toasts are ephemeral, so **every toast is also written to the Logs tab** (§8.6).
2. **UI goes through the host contract.** The Rust client never reads files or
   talks to managers. Python (`nexus/ui/ratatui/`) projects host results into the
   snapshot; Rust renders and sends operations back.
3. **Layering.** `nexus/ui/**` imports only the allowed packages
   (`tests/test_ui_layering.py`). Typed settings rows are built in
   `nexus/ui_support/` or `nexus/ui/ratatui/`, never in `host/`.
4. **Hover never changes geometry or focus** (existing rule from
   `TUI_COMPONENT_REDESIGN_PLAN.md`). Focus is changed only by keys or clicks.
5. **Everything is bounded**: toast queue length, list sizes, text lengths,
   animation redraw rate.
6. **Security posture unchanged**: secrets in text inputs are masked; API keys are
   sent to the daemon and never echoed back into the snapshot.

---

## 2. Step 0: remove the Textual mock-ups

`design-mockups/` is tracked on `main` (25 files: `pyproject.toml`, `uv.lock`,
`README.md`, `PLAN.md`, `src/design_mockups/**`). Other copies exist and are
**not** part of this repo's tree: leave them alone unless the user says so.

| What | Action |
| --- | --- |
| `design-mockups/` on `main` | `git rm -r design-mockups` (also delete the untracked `src/nexus_design_mockups.egg-info/`, `shots/`, `picks.json`, `.venv/` if present) |
| `docs/decisions.md` line ~149 ("The 26 elements in `design-mockups/` were judged…") | Keep the decisions; change the sentence to past tense and say the Textual mock-ups were removed in favour of `design-mockups/` (Ratatui), see this plan. |
| Worktree `/Users/santa/repos/nexus-design-mockups` (branch `feat/design-mockups`) | **Ask the user** before `git worktree remove` / deleting the branch. |
| `.claude/worktrees/shared-daemon-plan/design-mockups`, `artifacts/release-mcp/design-mockups` | Other worktrees; do not touch. |
| Any `.gitignore` entries for `design-mockups/shots`, `picks.json` | Replace with the new ones in §10.7. |

Commit: `chore: remove Textual design mock-ups`. Do this in its own commit so the
new project's history is clean.

---

## 3. Architecture: kit, mock-ups, real TUI

```
rust/
  widgets/                    NEW crate "nexus-widgets" (library, no IO)
    Cargo.toml                ratatui = "0.29", unicode-width, unicode-segmentation
    src/
      lib.rs                  re-exports; crate docs state the contract
      theme.rs                Tokens (colour roles), Theme::dark()/light()/mono()
      glyphs.rs               Glyphs::unicode()/ascii()
      focus.rs                FocusId, FocusRing, FocusScope, Nav result
      keys.rs                 Key → Intent mapping (Up/Down/Activate/Toggle/…)
      hit.rs                  HitMap: Rect → (FocusId, Part) for mouse
      anim.rs                 bounded hover/toast timers (Instant-based)
      layout.rs               columns(), rows(), inset(), responsive breakpoints
      components/
        button.rs  icon_button.rs  toggle.rs  checkbox.rs  radio.rs
        segmented.rs  tabs.rs  select.rs  combobox.rs  text_input.rs
        stepper.rs  ordered_list.rs  list.rs  table.rs  tree.rs
        section.rs  setting_row.rs  kv.rs  badge.rs  status_dot.rs
        meter.rs  spinner.rs  scrollbar.rs  toast.rs  modal.rs
        confirm.rs  key_hints.rs  callout.rs  empty.rs  search_field.rs
        split.rs  drawer.rs  tooltip.rs
  tui/                        existing; gains `nexus-widgets = { path = "../widgets" }`
  desktop/                    untouched

design-mockups/               NEW standalone Cargo binary "nexus-mockups"
  Cargo.toml                  nexus-widgets = { path = "../rust/widgets" }, ratatui, crossterm
  README.md
  src/
    main.rs                   CLI: `run`, `gallery`, `screen <name>`, `shoot`
    fixture.rs                one fake world (sessions, providers, models, agents…)
    viewer.rs                 viewer chrome: screen switcher, state switcher, theme
    screens/                  one file per screen (§9)
    shoot.rs                  TestBackend → .txt + .svg + index.html
  tests/
    screens.rs                every screen × state × size × theme renders, no panic
```

### Why a separate `rust/widgets` crate
- The user wants components; the mock-ups must use the **same** components the
  TUI will use, otherwise the mock-ups drift (the Textual ones could never be
  reused because they were Python/Textual and the TUI is Rust).
- `design-mockups/` stays deletable: deleting it removes no product code.
- `rust/tui/src/render/components.rs` (today: `button`, `toggle`, `selectable`,
  `section`, `mix`, hover state) is **moved into** `nexus-widgets` in Phase 2, not
  duplicated. Until then, the kit must keep the same colour maths (`mix`) and the
  100 ms hover duration so screenshots match.
- No Cargo workspace is introduced (keeps `rust/tui/Cargo.lock` where it is). Both
  consumers use a path dependency. If a workspace is wanted later, that is a
  separate change.
- **Not a Python module**, so `tests/test_docs.py`'s module-map rule does not
  apply; still add rows to `docs/module-map.md` for discoverability (§14).

### How the kit is used (immediate-mode, like Ratatui)
Components are **stateless render functions plus small state structs** the caller
owns. Example shape (illustrative, not final API):

```rust
pub struct ToggleProps<'a> {
    pub id: FocusId,
    pub label: &'a str,          // shown; never empty
    pub on: bool,
    pub locked: Option<&'a str>, // Some(reason) → drawn LOCKED, reason in hint bar
    pub disabled: bool,
}
pub fn toggle(f: &mut Frame, area: Rect, props: &ToggleProps, ui: &mut Ui) -> Response;

pub struct Ui<'t> {               // passed to every component each frame
    pub theme: &'t Theme,
    pub glyphs: &'t Glyphs,
    pub focus: &'t mut FocusRing, // registers id, knows if focused
    pub hits: &'t mut HitMap,     // registers clickable rects (same rects as drawn)
    pub hover: &'t HoverState,    // per-id 0.0..1.0 blend
    pub now: Instant,
}
pub struct Response { pub focused: bool, pub hovered: bool, pub rect: Rect }
```

Input is handled **after** layout by the owner: `FocusRing::handle(key) ->
Option<Intent>` and `HitMap::at(x, y) -> Option<(FocusId, Part)>`. Components
never mutate application state themselves; they return intents
(`Intent::Toggle(id)`, `Intent::Activate(id)`, `Intent::Move(id, Up)`, …) and the
owner turns intents into host operations. This mirrors today's design where Rust
sends `operation` JSON back to Python.

---

## 4. Design principles

1. **Keyboard first, mouse equal.** Every control is reachable by keys; every
   clickable rect is also a focus stop (except toasts' `×`, see §8.4).
2. **One visible focus.** Exactly one element shows the focus style at a time,
   across sidebars, page, dialogs. Focus is a *ring* (accent-coloured left bar
   `▌` + bold label), not colour alone, so it survives `NO_COLOR`.
3. **Flat structure.** Prefer one scrolling page with sections over nested pages.
   A drill-in is allowed only for (a) editing a file body, (b) a destructive
   confirmation, (c) a picker with more than ~12 choices (model picker).
4. **Label, value, where it's saved.** Every setting row shows: label, current
   value as a control, one-line description, and a **scope badge** (`global`,
   `project`, `session`, `default`). This is the "what is going where" fix from
   `SETTINGS_REVAMP_PLAN.md` §1.8.
5. **Immediate apply, visible confirmation.** Toggles/selects save immediately and
   raise a success toast naming the file (`Saved · ~/.nexus/nexus.toml`). Text
   fields save on Enter/blur. No global "Save" button.
6. **No silent truncation.** Lists say `… N more`; long values ellipsize with the
   full value available in the row's detail line / tooltip / details panel.
7. **Geometry is stable.** Hover, focus, loading and toasts never reflow the page.
   Toasts float over content (§8). Loading shows a spinner in place of the value.
8. **Responsive at fixed breakpoints**: `narrow < 90 cols ≤ medium < 140 ≤ wide`.
   Minimum supported terminal: 80×24. Every screen has a defined narrow layout.
9. **Monochrome-safe.** Meaning never depends on colour alone: toggles show
   `ON/OFF`, status dots have a letter fallback, errors have `!` prefix.
10. **Quiet by default.** Body text uses `text`; metadata uses `muted`/`quiet`;
    accent is reserved for focus, the primary action and the active choice.

---

## 5. Design tokens: colour, glyphs, spacing

### 5.1 Colour roles
Start from the existing `render::Palette` (dark background `#0B0B0B`, accent
orange, build blue `#5C9CF5`) so the redesign is an evolution, not a reskin. New
roles are marked NEW.

| Role | Use | Dark | Light |
| --- | --- | --- | --- |
| `bg` | app background | `#0B0B0B` | `#FFFFFF` |
| `surface` (= `panel`) | sidebars, composer card | existing `panel` | existing |
| `raised` (= `dialog`) | modals, toasts | existing `dialog` | existing |
| `element` / `element_hi` | control fill / hover fill | existing | existing |
| `text` / `muted` / `quiet` | body / metadata / disabled | existing | existing |
| `border` / `border_strong` | separators / focused container | existing | existing |
| `accent` | focus ring, primary button, active choice | existing orange | existing |
| `focus_bg` NEW | focused row background | `mix(surface, accent, 0.12)` | `mix(surface, accent, 0.10)` |
| `success` / `warning` / `error` / `blue` / `purple` / `cyan` | status | existing | existing |
| `toast_info` NEW | toast left bar | `blue` | `blue` |
| `toast_ok` NEW | | `success` | `success` |
| `toast_warn` NEW | | `warning` | `warning` |
| `toast_err` NEW | | `error` | `error` |
| `scope_global` / `scope_project` / `scope_session` NEW | scope badges | `purple` / `cyan` / `blue` | same hues, light variants |

A third theme `mono` NEW is used when `NO_COLOR` is set or the terminal reports
fewer than 256 colours: only `Modifier::{BOLD, DIM, REVERSED, UNDERLINED}`.

### 5.2 Glyphs (with ASCII fallback)
Selected via `Glyphs::unicode()` / `Glyphs::ascii()`; ASCII is used when
`NEXUS_ASCII=1` (NEW env var; document in `docs/config.md`) or `TERM=linux`.

| Name | Unicode | ASCII | Where |
| --- | --- | --- | --- |
| focus bar | `▌` | `>` | left of focused row |
| toggle on / off | `[■ ON ]` / `[ OFF □]` | `[x ON ]` / `[  OFF]` | Toggle |
| checkbox | `☑` `☐` `⊟` (mixed) | `[x]` `[ ]` `[-]` | Checkbox |
| radio | `◉` `○` | `(*)` `( )` | Radio |
| select caret | `▾` | `v` | Select |
| reorder handle | `⋮⋮` | `::` | OrderedList |
| up / down | `↑` `↓` | `^` `v` | stepper, move buttons |
| close | `×` | `x` | toasts, modals, tabs |
| status dots | `●` `◐` `○` `✕` | `*` `~` `o` `x` | status |
| chevrons | `▸` `▾` | `>` `v` | collapsible sections |
| ellipsis | `…` | `...` | clipping |
| lock | `🔒` is NOT used (emoji width varies); use text `LOCKED` | `LOCKED` | locked controls |

Rule: no emoji, no glyphs with ambiguous East-Asian width. Every glyph's width is
asserted to be 1 in a unit test (`unicode-width`).

### 5.3 Spacing and sizes
- Unit is one cell. Horizontal padding inside controls: 1. Between label and
  control: at least 2. Section gap: 1 blank row. Page gutter: 2 (wide), 1 (narrow).
- Control widths are fixed per type so columns align across a page:
  Toggle 9, Select min 14 max 32, Segmented = sum of labels + 3 per segment,
  Button = label + 4.
- Settings page: label column = `min(36, 40% of page)`; control column
  right-aligned; description on the next line in `muted`, indented to the label.

---

## 6. Focus and keyboard model

This section is the core of R6. Implement it in `nexus-widgets::focus` first;
every component depends on it.

### 6.1 Focus regions (the "where am I" layer)
The screen is divided into **regions**. Exactly one region is active; inside it,
exactly one element is focused.

| Region | Contents |
| --- | --- |
| `Composer` | the editor and its control row (agent, model, effort, mic) |
| `Transcript` | rows that can expand/collapse, context header chips |
| `SessionsSidebar` | search field, filter tabs, session rows, row actions |
| `DetailsSidebar` | tabs and their rows |
| `Overlay` | the top-most modal/dialog/Settings (a focus **trap**) |
| `Toasts` | not a Tab region; see §8.4 |

Region keys (global, work everywhere except inside a text input where noted):

| Key | Does |
| --- | --- |
| `Ctrl+B` | open the sessions sidebar **and focus it**; if already focused, close it and return focus to where it came from |
| `Ctrl+L` | same for the details sidebar |
| `F6` / `Shift+F6` NEW | cycle regions forward/back (Composer → Transcript → Sidebars → Composer). F6 is the conventional "next pane" key and is free today |
| `Esc` | leave the current region/overlay one level (§6.4) |
| `Ctrl+,` NEW (if the terminal reports it; fallback `/settings`) | open Settings |

### 6.2 Moving inside a region

| Key | Does |
| --- | --- |
| `↑` / `↓` | previous / next focusable row (skips headings and disabled rows, but **stops** on locked rows so the reason can be read) |
| `←` / `→` | inside a composite control (segmented, tabs, radio row, select closed): previous/next option. In a two-pane screen (Settings nav ↔ page, master ↔ detail) on a non-composite row: move between panes |
| `Tab` / `Shift+Tab` | next/previous focus stop **including** controls inside a row (e.g. a provider section's buttons). In the composer, Tab keeps its completion behaviour; Shift+Tab keeps cycling agent mode as today |
| `Home` / `End`, `PgUp` / `PgDn` | first/last, page jumps |
| `Enter` | activate (button, open select, open row detail, commit text) |
| `Space` | toggle (toggle, checkbox, radio); on a button same as Enter |
| `/` | focus the region's search field if it has one (sessions, settings, model picker, palette) |
| type a letter | **type-ahead** in lists without a search field: jumps to the next row starting with that letter (resets after 800 ms) |
| `Alt+↑` / `Alt+↓` (fallback `K`/`J` with Shift) | move the focused item in an **OrderedList** |
| `Delete` / `Backspace` | remove the focused item in an OrderedList (with undo toast, §8) |
| `?` | open the **key hint sheet** for the current region (not in text inputs) |

### 6.3 Focus visuals
```
  ▌ Theme                         [ Dark │ Light │ System ]     global
     Applies to this window and new windows.
```
- Focused row: `▌` in `accent` at column 0 of the row, row background `focus_bg`,
  label bold.
- Focused control inside a focused row (when Tab moved into the control): the
  control itself gets the focus ring (segmented: the focused segment is underlined
  + bold; button: reversed).
- Hovered but not focused: background blends to `element_hi` (existing 100 ms
  hover), **no** `▌`.

### 6.4 Esc ladder (single source of truth)
Esc always does the *least destructive* thing, in this order:
1. Close an open dropdown / completion popup.
2. Clear a non-empty search field (second Esc leaves the field).
3. Leave an inner control back to its row (Tab-focused control → row).
4. Close the top overlay (Settings, modal, picker) and **restore focus** to the
   element that opened it.
5. Return from a sidebar region to the Composer (sidebar stays open).
6. In the Composer: existing behaviour (stop hint / interrupt; dictation finish).

The ladder is implemented once in `FocusRing::escape()` and unit-tested.

### 6.5 Focus restoration and memory
- Every overlay records `opener: FocusId` and restores it on close.
- Each region remembers its last focused id across open/close
  (`HashMap<Region, FocusId>`), so `Ctrl+B` returns to the same session row.
- Ids are **stable strings** derived from data (`session:<id>`,
  `setting:<area>:<key>`, `provider:<id>:signin`), never indices, so a refresh
  that reorders rows does not move focus to a different item. If the id vanished,
  focus moves to the nearest surviving neighbour by previous index.

### 6.6 Key hint bar
Every overlay and focused sidebar shows a one-row **KeyHints** bar at its bottom,
generated from the focused element's intents, e.g.:

```
 ↑↓ move   ←→ tier   Space toggle   Alt+↑↓ reorder   Enter open   / search   ? keys   Esc close
```
Hints are truncated from the right with `…` and the full list is in `?`.

---

## 7. Component catalogue

For each component: anatomy, states, keys, mouse, sizing, API notes, tests. All
states listed must appear in the **gallery** screen (§10.3).

Common states for every focusable component: `default`, `hover`, `focused`,
`focused+hover`, `disabled`, and where relevant `locked(reason)`,
`loading`, `error(message)`.

### 7.1 Button
```
 [ Sign in with browser ]   [ Device code ]   [ Disconnect ]   [ Retry ]
   primary (accent fill)      secondary         danger            ghost
```
- Variants: `Primary` (accent bg, bg-coloured text; one per section max),
  `Secondary` (element fill), `Danger` (error text, element fill; requires a
  Confirm unless `confirm: false`), `Ghost` (no fill, underlined on focus).
- States: focused = reversed + bold; disabled = `quiet` text, no fill;
  loading = label replaced by spinner + same width (no reflow).
- Keys: Enter / Space. Mouse: click inside the bracketed rect.
- Optional **mnemonic**: one letter rendered underlined; `Alt+letter` activates it
  while its section is focused. NEW, low priority.

### 7.2 IconButton
`[×]`, `[↑]`, `[↓]`, `[+]`, `[⋯]`. Always 3 cells. Has a required `label` used in
the hint bar and `?` sheet (so `×` is announced as "Dismiss").

### 7.3 Toggle (switch)
```
 Voice input                                   [■ ON ]
 Send transcript automatically                 [ OFF□]
 Tool: bash                                    [LOCKED]   locked after first turn
```
- Fixed 9 cells; `ON` in success colour, `OFF` in muted. Locked shows `LOCKED` in
  quiet and the reason in the description line and hint bar.
- Keys: Space or Enter toggles. Mouse: click toggles; clicking the label focuses
  the row only (matches today's rule: switch = change, label = inspect).

### 7.4 Checkbox and CheckboxGroup
For multi-select (e.g. "Show providers: ☑ Connected ☐ Not connected"). Supports
mixed state `⊟` for a parent of a group (tool family with some tools off).

### 7.5 Radio group
Vertical list for 3–7 mutually exclusive options **with descriptions**:
```
 Run mode
   ◉ Tier          pick a model automatically from connected providers
   ○ Specific model  use an ordered list of models with fallbacks
```
↑↓ moves *and selects* (standard radio behaviour); Enter does nothing extra.

### 7.6 Segmented control
Horizontal, 2–5 short options, for values where all options fit on one line:
```
 [ Dark │ Light │ System ]       focused segment: underlined bold; active: accent fill
```
←→ moves *and selects*. Overflows → fall back to Select automatically at narrow
widths (the component does this; caller doesn't care).

### 7.7 Tabs
Like Segmented but switches **content below it**, not a value:
```
  Low   Medium   High
 ━━━━━─────────────────────────────────────────────
```
Active tab: accent text + heavy underline under its label. ←→ switch when the tab
strip is focused; `Ctrl+PgUp/PgDn` NEW switches from anywhere inside the tab
content; `1`/`2`/`3` switch when the strip is focused. Each tab may carry a badge
(`Medium ●` = has unsaved/invalid state, `High 3` = count).

### 7.8 Select (dropdown)
```
 Processing device          [ auto              ▾]
                            ┌───────────────────┐
                            │▌auto   (default)  │
                            │  cpu              │
                            │  metal            │
                            └───────────────────┘
```
Enter opens; ↑↓ moves; Enter picks; Esc closes without change; type-ahead works.
Popup opens below, or above if no room; max 10 rows then scrolls with a
scrollbar and `… N more` announcement. Current value marked `●`.

### 7.9 Combobox (searchable select)
Select with a filter field at the top of the popup. Used for > 10 options
(provider models, voices). Uses the shared fuzzy matcher (`ui_support/fuzzy.py`
semantics are already ported for the native palette; reuse that Rust code).

### 7.10 TextInput
Single line; variants `plain`, `secret` (shows `••••••` + last 4 chars only when
the daemon reports them; never the full key), `path`, `number`.
- Enter commits; Esc reverts; blur commits. Inline validation error under the
  field in `error` with `!` prefix. Ctrl+U/Ctrl+K/Ctrl+W/Alt+B/F as in composer.
- Uses the existing `editor.rs` grapheme logic (move the single-line subset into
  the kit; keep the composer's multi-line editor where it is).

### 7.11 Stepper (number)
`Recording limit   [ − ]  60 s  [ + ]` ; ←→ or −/+ change by step; Shift = ×10;
bounds shown in description (`10–300 s`).

### 7.12 OrderedList (reorderable) — key for Models (R4a)
```
 ┌ Medium ───────────────────────────────────────────────────── 3 models ┐
 │ ▌1  ⋮⋮  anthropic/claude-sonnet-5-5         in use        [↑][↓][×]  │
 │   2  ⋮⋮  openai/gpt-6-mini                    fallback      [↑][↓][×]  │
 │   3  ⋮⋮  google/gemini-3-flash                fallback · not connected │
 │      [ + Add model… ]                                                  │
 └─────────────────────────────────────────────────────────────────────────┘
```
- Row 1 is labelled `in use` (the model that actually runs); others `fallback`.
  A model whose provider is not connected shows `not connected` in warning and is
  skipped at runtime — say so in the row (this is the bug "low names the right
  model, but opening it shows another" from `SETTINGS_REVAMP_PLAN.md` §1.5).
- Keys: Alt+↑/↓ move; Delete removes (undo toast); Enter on `+ Add model…`
  opens the model picker (the one allowed drill-in, §4.3); `[↑][↓][×]` are Tab
  stops for mouse-less discoverability.
- Every move saves immediately; toast `Medium tier order saved`.

### 7.13 List (virtualised)
Generic selectable list used by sessions, palette, pickers. Supports group
headings, two-line rows (title + sub), right-aligned meta, multi-select (Space)
NEW, and per-row inline actions revealed on focus (`[Open] [Rename] [Archive]`).
Virtualised: renders only visible rows; scroll position keeps focus visible with
a 2-row margin.

### 7.14 Table
Columns with min/max widths and a `flex` column; header row; sortable columns
(`s` cycles sort on focused column) NEW; used by Keyboard, Tools, Usage.

### 7.15 Tree
Expand/collapse with → / ←, used by tool families and agent tool lists. Parent
rows can carry a Checkbox with mixed state.

### 7.16 Section (collapsible)
```
 ▾ OpenAI                                   ● connected · 34 models · api key
   …section body…
 ▸ Google                                   ○ not connected
```
Header is focusable; Enter / → / ← expand/collapse; collapsed state is
remembered per section id for the session of the window. Header shows a summary
so collapsed sections still show their state (no hidden information).

### 7.17 SettingRow (composite)
The building block of every Settings page:
```
 ▌ Send transcript automatically                   [ OFF□]      global
     Submit dictation immediately; off lets you review it in the composer.
```
Props: `id, label, description, control (one of 7.3–7.12), scope, locked,
error, saved_at (for a 2 s "saved ✓" flash in the scope column)`. The row is one
focus stop; Tab enters the control only when the control is composite (buttons
pair, ordered list).

### 7.18 KeyValue
Read-only `label · value` rows with aligned values, copy on `c` NEW (copies the
value, toast `Copied`). Used in details sidebar, provider account info, model
details.

### 7.19 Badge / Chip, StatusDot
Badges: `global` `project` `session` `default` `built-in` `edited` `new`
`beta`. StatusDot: `● ok`, `◐ working`, `○ idle/off`, `✕ error` with fixed
colours and ASCII fallback.

### 7.20 Meter and Progress
Meter (context usage with threshold marks, reuse current `context_marks` logic),
Progress (determinate `▕████▌    ▏ 46%  82/179 MB`), Spinner (braille, 80 ms
frame, only redraws while visible).

### 7.21 Scrollbar
Thin `│` track in `border`, `┃` thumb in `muted`; appears only when content
overflows; clickable/drag-able (mouse) and reflects keyboard scrolling.

### 7.22 Modal / Dialog / Drawer
- Modal: centred, `raised` background, title left, `[×]` and `esc` right, KeyHints
  bottom. Sizes: `sm` 56×12, `md` 80×24, `lg` = conversation area inset 2.
- Drawer: slides from left/right (no animation, just appears), used for
  sidebars at narrow widths (§9.2).
- Confirm: modal `sm` with message, consequence line ("Removed files move to
  trash"), `[ Cancel ]` focused by default, `[ Delete ]` danger. `y`/`n` shortcuts.

### 7.23 Toast — see §8.

### 7.24 Callout
Inline, full-width message inside a page (not floating):
`! Google is not connected — models from it are skipped.   [ Connect ]`.
Use a callout (not a toast) for **persistent** conditions; toasts are for
**events**.

### 7.25 EmptyState
Centred icon-less message + one primary action:
`No archived sessions.   [ Show active ]`.

### 7.26 SearchField
TextInput variant with `⌕`/`/` prefix, match count on the right (`12 of 48`), and
Esc ladder behaviour (§6.4).

### 7.27 Tooltip / description footer
Terminals have no real tooltips. Instead every overlay has a 1-row **description
footer** above the KeyHints showing the focused element's full description /
full untruncated value. Hover also fills it (no geometry change).

---

## 8. Toasts (replacing "notice")

Today `shell.notice` is a single string. `prototype.py` renders it into the
transcript lines as `Error: …` and as a `{"id": "notice", "title": "Notice"}`
block; many unrelated messages use it ("Copied 120 characters", "Voice off",
"No background tasks", exceptions, reconnect messages).

### 8.1 Anatomy
```
                                                    ┌────────────────────────────────────┐
                                                    ▌ ✓ Saved                         [×] │
                                                    ▌   ~/.nexus/nexus.toml · [voice]     │
                                                    └────────────────────────────────────┘
                                                    ┌────────────────────────────────────┐
                                                    ▌ ! Could not reach OpenAI      [×] │
                                                    ▌   HTTP 503 · retrying in 8 s        │
                                                    ▌   [ Details ]                       │
                                                    └────────────────────────────────────┘
```
- Floats at the **top-right of the conversation area**, under the top bar, 2
  cells from the right edge; width `min(48, 40% of width)`, min 32. At narrow
  widths: full conversation width minus 2, still top. Stacks downward, newest on
  top. Never covers the composer.
- Left bar `▌` in the level colour; level glyph `i` / `✓` / `!` / `✕`
  (ASCII-safe). Title bold, optional one-line body in `muted`, optional one
  action button.
- `[×]` at the top-right of every toast (R7).

### 8.2 Levels and lifetimes

| Level | Example | Auto-dismiss |
| --- | --- | --- |
| `info` | "Full tool output previews on", "Copied 120 characters" | 4 s |
| `success` | "Saved · ~/.nexus/nexus.toml" | 3 s |
| `warning` | "UI background actions busy; try again" | 8 s |
| `error` | exceptions, projection failures, "Display update failed" | 12 s, and **never** while hovered or while its action is focused |

- Timers **pause** while the mouse is over the toast, and while the terminal is
  unfocused if focus events are available (crossterm `FocusLost`).
- A progress hairline NEW: the bottom border of the toast shortens as time runs
  out (`└──────────────────┘` → `└──────────┘`), redrawn at most 4×/s, only while a
  toast is visible.
- Max **3** visible; a 4th collapses the oldest into `+2 more` (a one-row toast
  that opens the notification list when activated).
- **Dedup**: same `key` within 2 s replaces the existing toast and shows `×2`.

### 8.3 Not toasts (stay where they are)
- **Disconnected** state: persistent banner + disabled composer (existing). A
  toast is raised on *transition* ("Reconnected · durable state replayed").
- **Update available**: stays a persistent chip in the footer (existing
  `update_notice`), because it is a state, not an event.
- Permission prompts, questions: inline prompts (existing), never toasts.
- Turn errors that belong to the conversation: stay in the transcript (durable).

### 8.4 Keyboard and mouse
- Toasts are **not** in the Tab order (they would steal focus from typing).
- `Ctrl+X X` (leader, then `x`) dismisses **all** visible toasts. Verify the
  binding is free in `rust/tui/src/input.rs`; the leader currently uses `M`, `V`,
  `U`.
- `Ctrl+X T` NEW: focus the newest toast's action button (if it has one); Enter
  runs it, Esc returns focus.
- Mouse: click `[×]` dismisses that toast; click the body runs the action if any,
  else does nothing; wheel over toasts scrolls the content beneath.
- Esc does **not** dismiss toasts (Esc already means stop/close; overloading it
  would make Esc-to-interrupt unpredictable).

### 8.5 Notifications list NEW
`Ctrl+X N` (or `/notifications`) opens a modal listing the last 50 toasts of this
window with time, level and full text. This is how a dismissed toast's content
stays reachable, together with the Logs tab.

### 8.6 Data flow
- Python: replace `shell.notice = "..."` with
  `shell.toast(text, level="info", key=None, body="", action=None)`. It appends
  `{id: <monotonic int>, level, title, body, key, action_operation, at}` to a
  bounded deque (len 20) and writes the same text to the client log at the
  matching log level (so the Logs tab shows it — invariant 1).
- Snapshot: new field `toasts: Vec<Toast>` (schema bump, §11.1). Python always
  sends the deque; Rust keeps `dismissed: HashSet<u64>` and `shown_at:
  HashMap<u64, Instant>` locally and owns all timers (no Python round-trip for
  expiry, matching how hover is local).
- Transitional compatibility: keep `shell.notice` as a property whose setter
  calls `toast(..., level="error" if text.startswith(("Error", "Disconnected"))
  else "info")` until every call site is migrated; then remove it and remove the
  `"notice"` transcript block in `prototype.py` (lines ~671–674).
- Level mapping for current call sites (grep `shell.notice` / `self.notice`):

| Call site | Level |
| --- | --- |
| `background.py:29` busy | warning |
| `background.py:73/78` copied | success |
| `background.py:85` exception | error |
| `actions.py:267` exception | error |
| `actions.py:411` no session matching | warning |
| `actions.py:506/511` nothing to show | info |
| `actions.py:559/568/584/591` | info |
| `prototype.py:850/852` disconnect/reconnect | (banner) / success |
| `prototype.py:926` display update failed | error |
| projection section failures (`prototype.py:552`, `:591`) | error, key = section name |

---

## 9. Screens

All mock-ups below are at **120×36** unless marked. `▌` marks focus. Real
spacing in the mock-up project wins over these sketches; these define structure,
content and behaviour.

### 9.1 Main shell (chat)
Mostly today's layout, adjusted to host toasts and focus regions.

```
 ▌ New Session ● │ Fix token refresh ◐ │ Docs pass ●  +                                                    ▐
  ~/repos/nexus  ·  main  ·  worktree: none                                         build · sonnet-5.5 · 41%
 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                    ┌──────────────────────────────────┐
     ◈ System prompt ·························· 2,140 tok           ▌ ✓ Copied 1,204 characters  [×] │
     ◈ AGENTS.md ······························ 3,880 tok           └──────────────────────────────────┘
     ◈ Tools · 24 ····························· 6,010 tok
     ◈ Skills · 7 ····························· 1,120 tok
                                                         Context total · ~13,150 tokens
    ┃ Fix the token refresh race in auth/session.py
    ✓ Read 3 files · Ran 2 commands · Edited 1 file · Thought 2 times
    The race happens because …
 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
  ▎ Ask anything…                                                                                          
  ▎                                                                                                        
  ▎ build ▾   sonnet-5.5 ▾   medium ▾                                                ◐ 41% · 82k/200k   🎙
   ↑↓ history   Enter send   Shift+Enter newline   Ctrl+B sessions   Ctrl+L details   F6 next pane   ? keys
```
Changes vs today:
- The composer control row (`build ▾ sonnet-5.5 ▾ medium ▾`) are **Select**
  components: Tab from the editor (when no completion is open) → `Shift+Tab` is
  taken, so use `Ctrl+X M` (model, existing), `Ctrl+X A` NEW (agent), `Ctrl+X E`
  NEW (effort). Mouse click opens them as today.
- NEW optional bottom hint row (setting **Layout → Show key hints**, default on for
  the first 10 launches, then off; stored in `ui_support/preferences`).
- Mic glyph: keep current text glyph; do not use emoji in real implementation
  (`🎙` above is illustration only; use `mic`/`●` as today).

### 9.2 Sessions: one surface for `/session`, `/sessions`, `Ctrl+B` (R5)
**Decision:** there is one Sessions surface, the **left sidebar**. `/session`,
`/sessions`, `Ctrl+B` and clicking the top-bar sidebar button all open it **and
focus its search field**. `/archived` (`/resume`) opens it with the `Archived`
filter. The separate "Sessions" menu dialog (`Workflows.sessions()` →
`self.menu("Sessions", …)`) is removed.

Wide/medium (sidebar docked, conversation shrinks):
```
 ┌ Sessions ─────────────────────── [×] ┐
 │ ⌕ refr▏                       3 of 41 │
 │  Active   All   Archived              │
 │ ━━━━━━                                │
 │ ~/repos/nexus                         │
 │ ▌◐ Fix token refresh           2m     │
 │    build · running · 4 turns          │
 │    [Open] [Rename] [Fork] [Archive]   │
 │  ● Refresh docs for models    1h      │
 │    finished · unread                  │
 │ ~/repos/site                          │
 │  ○ Refresh CSS tokens         3d      │
 │                                       │
 │  [ + New session ]                    │
 │ ↑↓ move  Enter open  Space select     │
 │ r rename  a archive  f fork  ? keys   │
 └───────────────────────────────────────┘
```
Narrow (< 90 cols): the same component as a **drawer overlay** over the
conversation, full height, width `min(44, 90%)`. Opening it does not resize the
transcript.

Behaviour:
- Rows: status dot (existing `status` values: running/unread/idle/error), title
  (`New Session` fallback), relative time, sub-line (existing `sub`), grouped by
  workspace (existing `group`). The current session is marked with accent title.
- Filter tabs: `Active` (default) / `All` / `Archived` — Tabs component, ←→ when
  the strip is focused, `Ctrl+PgUp/PgDn` from the list.
- Typing in the search field filters with the shared fuzzy matcher over title,
  workspace and id prefix; `↓` from the field enters the list; `↑` on the first
  row returns to the field.
- Focused row reveals inline actions; single-letter shortcuts work when the list
  (not the field) is focused: `Enter` open in this tab, `o` NEW open in a new tab,
  `r` rename (inline TextInput replaces the title), `f` fork, `a`
  archive/unarchive, `Delete` → Confirm (only for archived sessions; consistent
  with "closing a tab never deletes a session").
- Space toggles multi-select NEW; with ≥1 selected the hint bar shows
  `a archive 3   Esc clear selection`.
- Truncation: `sessions_truncated` → last row `… more sessions not loaded ·
  [ Load more ]` (needs `ProjectSessions` paging, §11.3; until then, the existing
  "[Session list truncated]" line).
- Esc ladder: clear search → leave to composer (sidebar stays docked on wide; the
  drawer closes on narrow). `Ctrl+B` again closes the sidebar.
- `/session <id>` with an argument keeps today's behaviour (switch directly); on
  no match, warning toast + sidebar opens with the search pre-filled.

### 9.3 Settings (R4, R4a, R4b)
Settings is an overlay (`lg` modal) with **three parts**: header, area list,
page. The page is **one scrolling column of sections**; no drill-in pages except
the four allowed in §4.3.

```
 ┌ Settings ───────────────────────────────────────────────────────────────────────────────── [×] esc ┐
 │ ⌕ Search all settings▏                                     Scope  [ Global │ Project ]              │
 ├──────────────────────┬──────────────────────────────────────────────────────────────────────────────┤
 │ GENERAL              │  Models                                                                       │
 │   Appearance         │  The model that runs is the first connected model in each list.               │
 │   Layout             │                                                                               │
 │   Keyboard           │  DEFAULT                                                                      │
 │ CONFIGURE            │ ▌ Default model chain                                              global     │
 │   Providers     2/5  │     1 anthropic/claude-sonnet-5-5   in use                    [↑][↓][×]       │
 │ ▌ Models             │     2 openai/gpt-6                  fallback                  [↑][↓][×]       │
 │   Agents        6    │     [ + Add model… ]                                                          │
 │   Tools        24    │   Default reasoning effort        [ low │ medium │ high ]       global       │
 │   MCP servers   3    │   Session titles                                         [■ ON ]  global      │
 │   Skills        7    │     Title model                   [ quick tier          ▾]                    │
 │   Voice & speech     │                                                                               │
 │                      │  TIERS                                                                        │
 │                      │    Low    Medium    High                                                      │
 │                      │   ━━━━━━─────────────────────────────────────────────────────                │
 │                      │     1 anthropic/claude-haiku-4-5   in use                    [↑][↓][×]        │
 │                      │     2 google/gemini-3-flash        fallback · not connected  [↑][↓][×]        │
 │                      │     [ + Add model… ]                                                          │
 │                      │                                                                               │
 │                      │  CATALOGUE                                                                    │
 │                      │   Model catalogue   412 models · refreshed 3h ago     [ Refresh ]             │
 ├──────────────────────┴──────────────────────────────────────────────────────────────────────────────┤
 │ The first model of each list runs; others are fallbacks, tried in order.  Saved in ~/.nexus/nexus.toml│
 │ ↑↓ move  ←→ pane  Alt+↑↓ reorder  Delete remove  Enter add  Ctrl+PgUp/PgDn tier  / search  Esc close  │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 9.3.1 Header
- **Search all settings** NEW: matches area names, section titles, row labels and
  descriptions across every area (index built from the typed page models, §11.2).
  Results replace the page with a flat list `Area › Section › Row`; Enter jumps to
  the row (focus + scroll + 1 s highlight). `/` focuses it from anywhere in
  Settings.
- **Scope** segmented control replaces the "Switch to project/global" rows. It is
  disabled with reason for areas that are global-only (Agents today: "Agents are
  global").
- Area list counts (`2/5` connected providers, `24` tools) are summaries, so the
  list is informative without opening each area.

#### 9.3.2 Area list
Kept from today (`SETTINGS_SECTIONS`), with these changes:
- **Removed areas** (already planned): Workspace, Config, Soul, Hooks.
- **Merged**: `Voice` + `Speech` → **Voice & speech** (two sections on one page).
- **Merged**: `Session titles` → section inside **Models** (it is a model choice).
- Focus model: ↑↓ move through areas and **switch the page immediately** (cheap:
  the page is a projection), → or Enter moves focus into the page, ← from a
  page row without composite focus returns to the area list (the exact ask in
  `SETTINGS_REVAMP_PLAN.md` §1.1).
- `Ctrl+1…9` NEW jump to the n-th area.

#### 9.3.3 Models page (R4a)
As drawn above. Sections, top to bottom:
1. **Default**: Default model chain (OrderedList), Default reasoning effort
   (Segmented), Session titles on/off (Toggle) + Title model (Select: a tier or a
   specific model).
2. **Tiers**: Tabs `Low | Medium | High`, each an OrderedList. Tab badge shows
   `!` if no model in that tier is connected. Under the list, one muted line:
   "Used by: quick, explore (agents)" — which agents run on this tier, so the
   user sees the effect.
3. **Catalogue**: count, last refresh, `[ Refresh ]` (existing `ModelsRefresh`),
   with a spinner while refreshing.

The model picker (Combobox in a `md` modal) is the only drill-in; on pick it
returns focus to the `+ Add model…` row with the new item focused.

#### 9.3.4 Providers page (R4b)
One page, one collapsible Section per provider; connected providers first, then
alphabetical. Collapsed by default except connected ones and the focused one.
```
  Providers                                                   Show  [ All │ Connected ]
  Credentials stay in the daemon (~/.nexus/credentials.json). Connect more than one to fall back.

 ▾ Anthropic                                         ● connected · OAuth · 18 models
     Account          you@example.com · Max plan
     Usage            52% of 5-hour limit · resets 14:20               [ Usage ]
     Methods          [ Sign in again ]   [ Use API key instead ]   [ Disconnect ]
 ▾ OpenAI                                            ● connected · API key ••••3f2a · 34 models
     Key              set in credentials.json                          [ Replace key ]
     Base URL         default                                          [ Edit ]
     Methods          [ Disconnect ]
 ▌▸ Google                                           ○ not connected
 ▸ OpenCode Go                                       ✕ error · token expired   [ Sign in ]
 ▸ Ollama (local)                                    ○ not running · localhost:11434
```
- Section summary line always shows status, auth method, model count, so a
  collapsed provider hides nothing important.
- Sign-in flows: `Sign in with browser` shows an inline **progress row** inside the
  section (spinner + "Waiting for browser… [ Cancel ]"); device code shows the
  code in large-ish bold with `[ Copy code ]` and the URL — inline, not a new page.
  API key: inline secret TextInput replaces the Methods row; Enter saves.
- Errors from sign-in show as a Callout inside the section **and** an error toast.
- Clicking or activating anything never closes Settings (fixes §2.2 of the
  revamp plan: no operation may call `shell.show(...)` or reset `settings_nav`;
  see §11.2 — the page model makes this structural, not a `NAV_KEEP` list).

#### 9.3.5 Agents page
Master–detail on one page (wide), stacked (narrow):
```
  Agents                                    New sessions start with  [ build          ▾]
 ┌──────────────────────┬──────────────────────────────────────────────────────────────┐
 │ ▌build      built-in │  build                                         built-in · edited │
 │  orchestrator        │  Runs on       ◉ Tier [ medium ▾]   ○ Specific model            │
 │  advisor             │  Effort        [ low │ medium │ high ]                           │
 │  task                │  Colour        ● blue  [ Change ▾]                               │
 │  quick               │  Tools         22 of 24 on            [ Choose… ]                 │
 │  reviewer   custom   │  Prompt        ~/.nexus/agents/build.md · 1,240 tok  [ Edit ]     │
 │                      │                [ Reset to built-in ]                              │
 │ [ + New agent ]      │                                                                   │
 └──────────────────────┴──────────────────────────────────────────────────────────────┘
```
- ↑↓ in the master list changes the detail immediately; → enters the detail.
- Run mode Radio: `Specific model` reveals an OrderedList in place (no new page).
- `Edit` opens the existing file editor (allowed drill-in (a)). `Choose…` opens a
  Tree of tools with checkboxes in a `md` modal (drill-in (c)).

#### 9.3.6 Tools page
Tree grouped by family with Checkbox per tool and mixed state per family; right
column token estimate; search field; LOCKED rows explain "Locked after the first
turn of this session". Scope badge per row.

#### 9.3.7 MCP servers page
One Section per server (same pattern as Providers):
```
 ▾ github                     ● running · 31 tools · ~4,100 tok          global
     Enabled            [■ ON ]
     Tool loading       [ search │ eager ]      search: tools are found on demand
     Command            npx -y @modelcontextprotocol/server-github
     Actions            [ Restart ]   [ Edit config ]   [ Remove ]
 ▸ postgres                   ✕ failed to start · exit 1   [ Logs ]     project
```
`[ + Add server ]` at the bottom opens the config editor with the starter body
(existing `new_file_body("mcp")`).

#### 9.3.8 Skills page
List with Toggle per skill + description + token estimate; Enter opens a
read-only Markdown preview modal (existing SettingsRead); `[ New skill ]`.

#### 9.3.9 Voice & speech page
Two sections, everything inline (today these are separate menus with
`voice_choices` sub-pages):
```
  VOICE INPUT (local dictation)
 ▌ Voice input                           [■ ON ]                    global
   Send transcript automatically         [ OFF□]                    global
     Off lets you review the transcript in the composer before sending.
   Processing device                     [ auto           ▾]        global
   Recording limit                       [ − ]  60 s  [ + ]         global
   Model                                 ● ready · whisper-small · 179 MB    [ Re-check ]

  SPEECH (/speak, local Kokoro)
   Language                              [ English (US)   ▾]        global
   Voice                                 [ af_heart       ▾]        global
   Speed                                 [ − ] 1.0×  [ + ]          global
   Device                                [ auto           ▾]        global
   Model                                 ○ not downloaded · ~330 MB  [ Download… ]
                                         [ Reset speech settings ]
```
Downloads show a Progress row in place; consent stays a Confirm (existing
consent text from `ui_support/speech_download.py`).

#### 9.3.10 Appearance, Layout, Keyboard
- Appearance: Theme Segmented (Dark/Light/System NEW), Glyphs Segmented
  (Unicode/ASCII NEW), Reduce motion Toggle NEW (disables spinners/hover blend/
  toast hairline), Dense transcript Toggle NEW.
- Layout: Sessions sidebar on start Toggle, Details sidebar on start Toggle,
  Sidebar widths Stepper, Show key hints Toggle NEW, Toast position Segmented
  (Top right / Bottom right) NEW.
- Keyboard: read-only Table `Action | Keys | Where`, searchable, grouped
  (Global, Composer, Lists, Settings, Sessions); replaces `/hotkeys` page content
  (keep `/hotkeys` as an alias that opens Settings → Keyboard).

### 9.4 Model picker (refresh, not redesign)
Keep today's centred `Select model` modal (one row per model, provider
discriminator, favourites, sort, Ctrl+I details). Rebuild on Combobox + List so
it shares focus/keys. Add the KeyHints bar from the kit (replacing ad-hoc
`panel_hint`).

### 9.5 Command palette (Ctrl+P)
Combobox modal grouped by `Commands`, `Sessions`, `Settings` NEW (settings search
results jump straight to a row), `Agents`. Each row: name, shortcut on the right,
one-line description.

### 9.6 Permission prompt and question
Inline above the composer (unchanged position). Use Buttons for the decisions
(`[ Allow once ]` primary, `[ Allow for session ]`, `[ Deny ]`), Radio for
questions, number keys `1–9` select (existing), labelled parameter block above
(existing `tool_details` rendering). Focus jumps to the prompt when it appears and
returns to the composer when answered.

### 9.7 Context inspection (Ctrl+I)
Existing large modal; adopt Section + Tree + Toggle components; KeyHints bar.

### 9.8 Details sidebar (Ctrl+L)
Tabs `Session | Files | Logs | MCP`; KeyValue rows; Files list with expandable
diffs; Logs tab gains a `Notices` filter (toasts history, §8.6).

### 9.9 Confirm, editor, usage, help
- Confirm: §7.22. Default focus Cancel. `y`/`n`.
- File editor: existing editor in a `lg` modal with `[ Save ] [ Cancel ]` buttons
  and `Ctrl+S` save; dirty marker `●` in title.
- Usage (Ctrl+U): Table per provider with Meters.
- `?` sheet: modal listing keys for the current region (generated from intents)
  with a link row `All shortcuts → Settings › Keyboard`.

### 9.10 Empty, loading, error, disconnected states
Each screen in the mock-ups must show: empty (EmptyState), loading (spinners in
place of values; skeleton rows `░░░░░░` NEW), error (Callout + toast), and
disconnected (banner; every control disabled with reason "Daemon unreachable").

### 9.11 Narrow layout (80×24)
- Sidebars become drawers (§9.2).
- Settings: area list collapses into a Select at the top of the page
  (`Area [ Models ▾]`); `Ctrl+PgUp/PgDn` still switch areas.
- Agents master–detail stacks: list, then detail below.
- SettingRow puts the control on the line under the label when
  `label + control + scope > width`.
- Toasts span the width and show title only (body in Notifications list).

---

## 10. The mock-up project

### 10.1 Purpose
Static data, real components, real keyboard handling. It answers "how does this
look and feel" before touching `rust/tui`, and its screenshots are the review
artefact. It never talks to the daemon and never imports `nexus` (Python).

### 10.2 Commands
```sh
cd design-mockups
cargo run                         # viewer, starts on the gallery
cargo run -- screen settings-models --theme light --size 80x24
cargo run -- list                 # screens, states, sizes
cargo run -- shoot                # write shots/ (txt + svg) and shots/index.html
cargo test                        # every screen × state × size × theme renders
```

### 10.3 Screens (each one a file in `src/screens/`)

| Key | Screen | States to show |
| --- | --- | --- |
| `gallery` | every component in every state, in a scrollable grid | all states in §7 |
| `chat` | main shell | idle, streaming, permission, question, recording, empty, disconnected |
| `chat-toasts` | toasts | one of each level, stacked 3 + "+2 more", hovered (paused), dedup ×2, narrow |
| `sessions` | sidebar docked | default, searching, archived tab, row actions, rename inline, multi-select, truncated, empty |
| `sessions-drawer` | narrow drawer | default, searching |
| `settings-appearance` | | default |
| `settings-layout` | | default |
| `settings-keyboard` | | default, filtered |
| `settings-providers` | | mixed connected/error, browser sign-in in progress, device code shown, API key entry, sign-in error |
| `settings-models` | | each tier tab, reorder in progress (moved row highlighted), not-connected warning, refresh in progress |
| `settings-agents` | | tier mode, specific-model mode, custom agent, global-only scope disabled |
| `settings-tools` | | mixed family, locked |
| `settings-mcp` | | running, failed, eager vs search |
| `settings-skills` | | default, preview modal |
| `settings-voice` | | ready, downloading, not downloaded, error |
| `settings-search` | | query with results across areas |
| `settings-narrow` | 80×24 | area Select |
| `model-picker` | | default, searching, details (Ctrl+I) |
| `palette` | | default, settings results |
| `confirm` | | danger |
| `notifications` | | list of 50 |
| `help` | `?` sheet | composer, sessions, settings |

### 10.4 Viewer keys (the viewer's own chrome, last row only)
| Key | Does |
| --- | --- |
| `F1` | viewer help |
| `F2` / `Shift+F2` | next / previous screen |
| `F3` | next state of this screen |
| `F4` | dark → light → mono |
| `F5` | size cycle: fit terminal → 80×24 → 120×36 → 200×50 (renders inside a frame) |
| `F7` | Unicode ↔ ASCII glyphs |
| `F8` | toggle "show focus ids" debug overlay |
| `Ctrl+Q` | quit |

Everything else goes to the **screen**, so arrow keys, Tab, Esc, Space, Enter,
`/`, Alt+↑↓ all behave as specified — the mock-ups double as a UX prototype.
Mutations act on an in-memory copy of the fixture (reorder a tier, toggle voice,
dismiss toasts) and a toast confirms "Saved (mock)". `F3`/state reset restores
the fixture.

### 10.5 Fixture (`fixture.rs`)
One fake world, used by every screen so differences come from design, not data:
- 3 workspaces, 41 sessions (running, unread, idle, error, archived, untitled,
  very long Unicode title `"Réparer le rafraîchissement du jeton — 東京 🚀"` (the
  emoji here is test data for width handling, not UI chrome)).
- 5 providers: Anthropic (OAuth, connected), OpenAI (API key, connected), Google
  (not connected), OpenCode Go (error: token expired), Ollama (not running).
- 412-model catalogue subset (≈60 real-looking rows incl. duplicate display
  names across providers).
- Tiers: low/medium/high with one not-connected fallback each.
- 6 agents (5 built-in, 1 custom), 24 tools in 5 families, 3 MCP servers
  (running, failed, disabled), 7 skills, voice ready, speech not downloaded.
- A transcript reused from the old Textual fixture's story (token-refresh fix:
  reads, greps, a diff, failing then passing pytest, parallel subagents, a policy
  denial, rate-limit retry, permission prompt, question, dictation).

### 10.6 Screenshots (`shoot.rs`)
- Render each screen × state × theme × size into Ratatui `TestBackend`.
- Write `shots/<screen>/<state>-<theme>-<WxH>.txt` (plain text) and `.svg` (one
  `<text>` per run of equal style, monospace font stack, cell size 8×17) — no new
  heavy deps; SVG is written by hand.
- `shots/index.html` (static, inline CSS) shows a grid per screen with theme/size
  filters. Open it locally or publish it if the user asks.
- Optional: the existing `skills/native-app-review` skill can still screenshot
  the real terminal when needed.

### 10.7 Repo hygiene
- `design-mockups/.gitignore`: `target/`, `shots/`.
- Not part of the Python package, the wheel, the test suite or CI (add a CI job
  only if asked). Deleting the folder removes it completely; `rust/widgets`
  remains and is product code.
- `README.md` in the folder: run commands, screen list, viewer keys, "how to add a
  screen", and the statement that it is deletable.

---

## 11. Host contract and bridge changes

These are needed to wire the redesign into `nexus chat` (Phases 3–5). Each has a
reason. Bump the snapshot `schema` once per phase that changes the wire format,
and keep Rust tolerant of missing fields (`#[serde(default)]`, as today).

### 11.1 Snapshot additions (`rust/tui/src/bridge.rs` + `nexus/ui/ratatui/prototype.py`)
```rust
pub toasts: Vec<Toast>,              // §8.6
pub settings_page: Option<Page>,     // §11.2, replaces nav/panel_lines/items for Settings
pub focus_request: Option<String>,   // Python asks Rust to focus an id once (e.g. after a save)

pub struct Toast { id: u64, level: String, title: String, body: String,
                   key: String, action_label: String, action: Option<Value>, at: f64 }
```

### 11.2 Typed Settings page model
Today Settings is `menu(title, rows, lines)` with label strings like
`"Voice input · on"` and `operation` dicts, plus a `NAV_KEEP` rule that decides
whether a page "is still Settings" (root cause candidate for "Settings closes
sometimes"). Replace it with a typed model built in Python:

```python
# nexus/ui_support/settings_page.py  (NEW; pure data, no IO; frozen msgspec structs)
class Control(msgspec.Struct, frozen=True, tag_field="kind"):  # union of:
    # toggle{on, locked_reason} | select{value, options[{value,label,detail}]}
    # segmented{value, options} | stepper{value, min, max, step, unit}
    # text{value, secret, placeholder} | ordered{items[{ref,label,status,note}]}
    # buttons{items[{id,label,variant,operation,confirm}]} | kv{value}
    # progress{fraction, label} | tabs{active, tabs[{id,label,badge,section}]}
class Row(msgspec.Struct, frozen=True):
    id: str; label: str; description: str; control: Control
    scope: str            # global|project|session|default|"" 
    operation: dict | None  # what Rust sends back; payload filled by the control
    error: str = ""
class Section(msgspec.Struct, frozen=True):
    id: str; title: str; summary: str; collapsible: bool; collapsed: bool; rows: tuple[Row, ...]
class Page(msgspec.Struct, frozen=True):
    area: str; title: str; intro: str; scope: str; scope_options: tuple[str, ...]
    scope_locked_reason: str; sections: tuple[Section, ...]; footer_path: str
```
- One builder per area in `nexus/ui/ratatui/settings_pages/` (`models.py`,
  `providers.py`, `agents.py`, `tools.py`, `mcp.py`, `skills.py`, `voice.py`,
  `appearance.py`, `layout.py`, `keyboard.py`), each calling existing host
  commands (`settings_inventory`, `providers_status`, `voice_status`,
  `speech_status`, `inspect_context`, model/tier settings via
  `host_support/model_settings.py` / `ui_support/tier_settings.py`).
- While `settings_page` is present, **Settings is open**. Operations from the page
  return a new `Page` (or an error toast) and never replace it with a generic
  panel. Leaving Settings is only Esc/×/`close_settings`. This makes "never close
  unexpectedly" structural and deletes `NAV_KEEP`.
- Search index: Python sends `settings_index: Vec<(area, section, row_id, label,
  description)>` once per Settings open (bounded: 2,000 rows) so Rust can search
  locally and request `{"kind": "settings_goto", "area", "row"}`.
- Existing `SETTINGS_SECTIONS` / `SETTINGS_HELP` in `ui_support/settings_help.py`
  remain the source of area order and help text.

### 11.3 Host commands possibly needed (verify first; add only if missing)
| Need | Check | If missing |
| --- | --- | --- |
| Session rename / archive / fork from the sidebar | `host/protocol.py` | add commands; handle in `facade.py` |
| Session list paging (`Load more`) | `ProjectSessions` has `truncated` only | add `cursor`/`limit` |
| Provider account/usage summary for the section header | `providers_status`, usage command | extend result struct, redacted |
| Agents tools-on count | settings inventory | derive in Python if possible |

Each addition: protocol + facade + `docs/host.md` + test in
`tests/test_host_facade.py`.

### 11.4 Sessions
- `/session`, `/sessions`, `/archived` in `ui/cli/commands.py` keep their names
  and aliases; their ratatui handlers set `shell.sessions_sidebar = True`,
  `shell.sessions_filter = "active"|"archived"` and `focus_request =
  "sessions:search"` instead of calling `Workflows.sessions()`.
- Remove `Workflows.sessions()` menu after the sidebar supports every action the
  menu had (open in workspace).

---

## 12. Phased implementation and checklists

Each phase is one PR (Conventional Commit title), with tests and docs in the same
PR. Do not start a phase until the previous one is merged or explicitly stacked.

### Phase 0 — remove old mock-ups (R1)
- [ ] `git rm -r design-mockups` + untracked leftovers; update `docs/decisions.md`.
- [ ] Ask about the `nexus-design-mockups` worktree/branch.
- Commit: `chore: remove Textual design mock-ups`.

### Phase 1 — widget kit foundation (R3, R6)
- [ ] Create `rust/widgets` (lib), `theme.rs`, `glyphs.rs`, `focus.rs`, `keys.rs`,
      `hit.rs`, `anim.rs`, `layout.rs`.
- [ ] FocusRing with regions, stable ids, Esc ladder, memory, restoration; tests.
- [ ] Components 7.1–7.27 with every state; unit tests render into `Buffer` and
      assert cells + styles; glyph width test.
- Acceptance: `cargo test -p` (in `rust/widgets`) green; `cargo clippy` clean.
- Commit: `feat: add nexus-widgets component kit for the native TUI`.

### Phase 2 — Ratatui mock-ups (R1, R2, R8, R9)
- [ ] `design-mockups/` crate, fixture, viewer, all screens in §10.3, shoot.
- [ ] `cargo test` renders every screen × state × {80×24, 120×36, 200×50} ×
      {dark, light, mono} without panic and without any row wider than the frame.
- [ ] Generate `shots/index.html`; review with the user **before Phase 3**.
- Commit: `feat: add Ratatui design mock-ups`.
- **Gate:** user picks/adjusts designs. Record decisions in `docs/decisions.md`.

### Phase 3 — kit into the real TUI + toasts (R7)
- [ ] `rust/tui` depends on `nexus-widgets`; move `render/components.rs` into the
      kit (keep a thin re-export to avoid a giant diff, then remove).
- [ ] `toasts` snapshot field, Python `shell.toast()`, migrate every
      `notice` call site (§8.6 table), remove the transcript `notice` block.
- [ ] Notifications list (`Ctrl+X N`, `/notifications` in `ui/cli/commands.py`).
- [ ] Toasts mirrored to the client log; Logs tab `Notices` filter.
- Tests: `tests/test_ratatui_actions.py` (toast levels), new
  `tests/test_ratatui_toasts.py`, Rust render/timer tests, PTY check.
- Commit: `feat: show TUI notices as dismissible toasts`.

### Phase 4 — Sessions surface (R5)
- [ ] Sidebar component with search, filter tabs, inline actions, rename,
      multi-select, drawer mode; `/session(s)`, `/archived`, `Ctrl+B` all open it.
- [ ] Host commands from §11.3 if missing.
- [ ] Remove the Sessions menu dialog.
- Tests: `tests/test_ratatui_workflows.py`, sidebar Rust tests, PTY check.
- Commit: `feat: unify /sessions and the sessions sidebar`.

### Phase 5 — Settings one-page model (R4, R4a, R4b)
- [ ] `ui_support/settings_page.py` + per-area builders; `settings_page` snapshot
      field; Rust renderer from the kit; Settings search; scope Segmented.
- [ ] Areas in this order (each a commit): Models (with Session titles), Providers,
      Voice & speech, Agents, Tools, MCP, Skills, Appearance/Layout/Keyboard.
- [ ] Delete `NAV_KEEP`, per-area `menu()` pages, `voice_choices`/`speech_choices`
      sub-pages, `provider_page` drill-in, tier drill-ins in `tier_pages.py`
      (logic moves into builders; keep saving code paths).
- Tests: one builder test per area (`tests/test_settings_page_<area>.py`),
      workflow tests for "no operation closes Settings", Rust page render tests,
      PTY: navigate every area with keys only.
- Commit(s): `feat: one-page Settings for <area>`.

### Phase 6 — keyboard polish everywhere (R6)
- [ ] F6 region cycling, `?` sheets, KeyHints bars, composer Selects
      (`Ctrl+X A/E`), type-ahead, Ctrl+1…9 in Settings.
- [ ] Keyboard page generated from the same intent table (single source).
- Commit: `feat: keyboard navigation across native TUI regions`.

### Requirement → phase map
| Req | Phases |
| --- | --- |
| R1 | 0, 2 |
| R2 | 2–6 |
| R3 | 1, 3 |
| R4 / R4a / R4b | 2 (design), 5 |
| R5 | 2, 4 |
| R6 | 1, 6 (and every phase's PTY check) |
| R7 | 2, 3 |
| R8 | 2 |
| R9 | NEW items throughout; cut freely at the Phase 2 gate |

---

## 13. Testing

### Rust (`rust/widgets`, `rust/tui`, `design-mockups`)
- Component render tests: render into `Buffer` at a fixed size; assert the text
  of each row and the style of key cells (focus bar colour, toggle text).
- Focus tests: a scripted sequence of keys over a fixture layout produces the
  expected focused ids (↑↓, ←→ pane switching, Tab into composite controls, Esc
  ladder, restoration after overlay close, id survival across reorder).
- Hit tests: every registered hit rect equals the drawn rect (reuse the rule
  "hover and click use the same rects").
- Toast timers: inject a fake clock; assert expiry, pause on hover, dedup, cap.
- Width safety: no rendered line exceeds the frame at 80×24 for any screen.

### Python
- Builders produce the expected `Page` for fixture host results (scripted
  provider + temp workspace, as other tests do).
- `shell.toast()` bounds and log mirroring; every former `notice` call site now
  emits the mapped level.
- "No operation closes Settings": iterate every operation kind a Settings page can
  emit and assert `settings_page` is still present afterward.
- `/session`, `/sessions`, `/archived` set sidebar + focus request.
- Layering tests still pass (`tests/test_layering.py`, `tests/test_ui_layering.py`).

### End-to-end
- Existing native PTY checks extended: keys-only walk through Settings areas,
  sessions sidebar search/open, toast appears and expires.
- Native screenshot review skill at 80×24 and 140×42, dark and light.
- Say "not verified" in docs for anything not run against a real terminal or a
  live provider.

Commands: `.venv/bin/python -m pytest -q`, `ruff check nexus tests`,
`cargo test` in `rust/widgets`, `rust/tui`, `design-mockups`.

---

## 14. Docs to update (same PR as the code)

| Doc | Change |
| --- | --- |
| `docs/ratatui-parity.md` | toasts, Sessions surface, one-page Settings, focus/regions, key hints |
| `docs/surfaces.md` | native-only differences (web not ported) |
| `docs/cli.md` | `/session(s)` opens the sidebar; `/notifications`; `/hotkeys` → Settings › Keyboard |
| `docs/decisions.md` | removal of Textual mock-ups; kit crate; toasts vs callouts vs banners; Settings page model; Esc does not dismiss toasts |
| `docs/module-map.md` | `rust/widgets/*`, `ui_support/settings_page.py`, `ui/ratatui/settings_pages/*` |
| `docs/config.md` | `NEXUS_ASCII`, new UI preferences (key hints, toast position, reduce motion, glyphs) |
| `docs/testing.md` | how to run widget/mock-up tests and shoot screenshots |
| `docs/devtools.md` | `design-mockups/` usage |
| `plans/SETTINGS_REVAMP_PLAN.md` | point §3.4 (typed-row visual design) at this plan |
| `plans/TUI_COMPONENT_REDESIGN_PLAN.md` | "remaining component migration" → this plan, Phase 3 |

---

## 15. Decisions taken here and open questions

### Decided (change at the Phase 2 gate if wanted)
1. Mock-ups are a Rust/Ratatui crate using a shared kit crate, not Python.
2. Sessions = the left sidebar; the Sessions dialog is removed.
3. Voice + Speech merge; Session titles moves into Models.
4. Settings scope is a header control, not rows.
5. Toasts top-right, 3 visible, levels with fixed lifetimes, `[×]`, `Ctrl+X X`
   dismiss all, Esc does not dismiss, history via `Ctrl+X N` and Logs.
6. Immediate save + success toast; no Save button except the file editor.
7. F6 for region cycling.

### Open questions for the user
1. Delete the `nexus-design-mockups` worktree and `feat/design-mockups` branch too?
2. Should error toasts be sticky (no auto-dismiss) instead of 12 s?
3. Toast position: top-right (proposed) or bottom-right above the composer?
4. Keep `/hotkeys` as its own overlay or always route to Settings › Keyboard?
5. Is a `mono` theme / `NEXUS_ASCII` worth shipping, or mock-up only?
6. Agents page: master–detail (proposed) or one Section per agent like Providers?

---

## 16. Appendices

### 16.1 Full keymap (proposed; verify conflicts in `rust/tui/src/input.rs` and `main.rs`)

| Scope | Key | Action |
| --- | --- | --- |
| Global | `Ctrl+B` | Sessions sidebar open+focus / close |
| Global | `Ctrl+L` | Details sidebar open+focus / close |
| Global | `F6` / `Shift+F6` | next / previous region |
| Global | `Ctrl+P` | command palette |
| Global | `Ctrl+I` | context inspection |
| Global | `Ctrl+U` | usage |
| Global | `Ctrl+X M` / `A` / `E` / `V` | model / agent / effort / dictation |
| Global | `Ctrl+X X` | dismiss all toasts |
| Global | `Ctrl+X N` | notifications list |
| Global | `Ctrl+X T` | focus newest toast action |
| Global | `?` (outside text input) | key sheet for the region |
| Lists | `↑↓ Home End PgUp PgDn` | move |
| Lists | `Enter` / `Space` | activate / toggle-or-select |
| Lists | `/` | search |
| Lists | letter | type-ahead (when no search field) |
| Composite | `←→` | option / tab / pane |
| Composite | `Tab` / `Shift+Tab` | next / previous stop incl. inner controls |
| Tabs | `Ctrl+PgUp/PgDn` | previous / next tab from inside content |
| Ordered list | `Alt+↑↓` | move item |
| Ordered list | `Delete` | remove item (undo toast) |
| Sessions | `o r f a Delete` | new tab / rename / fork / archive / delete archived |
| Settings | `Ctrl+1…9` | jump to area |
| Overlays | `Esc` | ladder (§6.4) |
| Confirm | `y` / `n` | confirm / cancel |

### 16.2 Glyph table
See §5.2. Unit test asserts `UnicodeWidthStr::width == 1` (or the declared
width) for every glyph in both sets.

### 16.3 Colour table
See §5.1. Light/dark values for new roles are derived with `mix()` from
existing roles so the kit has no hard-coded hex beyond today's palette.
