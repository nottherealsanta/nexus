# Plan: the Void design for NSH

Status: design final 2026-11-01. Implemented in NSH on 2026-11-01; the TUI is unchanged
(see [PLAN.md](PLAN.md) and the porting ledger).

This changes NSH only: web, desktop and iOS. **The TUI stays exactly as it is**, so do not edit
`nexus-tui/` or anything it reads. `AGENTS.md` asks the two clients to match, so record this as a
deliberate difference in `docs/porting-ledger.md` (step 14).

## Start here

1. **Keep the mock-up safe.** `nexus-design-mockup/stage/`, `interface/brand/` and this plan are untracked
   in the main checkout, so commit them first.
2. **Pick where to work.** Other work is uncommitted in this checkout (`EDITOR_GIT_PLAN.md`: `App.tsx`,
   `commands.ts`, `store.ts`, `centre.tsx`, `editor.tsx`, the manifest and `nexus-tui/`). Work on a branch,
   and don't include its files in your commits by accident.
3. **Reuse the partial build.** Most UI changes already exist in a worktree, so you can apply them
   instead of retyping them. See [The partial build](#the-partial-build).
4. **Run the baseline.** Note `nsh test` and `cargo test` results before you change anything. See
   [Testing](#testing).
5. **Work through the [steps](#work-order)**, with the mock-up open beside you.

## Where the design lives

Treat the mock-up as the visual spec, and this page as the instructions. If the two
disagree, this page wins.

| Thing | Where |
| --- | --- |
| Rendered mock-up, built | `nexus-design-mockup/stage/dist/index.html` (open it from disk) |
| Same page, hosted and private to you | <https://claude.ai/artifact/6LSmk9G6dGZW7Xp6EzrzE8> |
| NSH parts, drawn from tokens | `nexus-design-mockup/stage/src/parts.tsx` |
| Tokens and component CSS | `nexus-design-mockup/stage/src/nexus.css` |
| The list of surfaces and the animated turn | `nexus-design-mockup/stage/src/main.tsx` (`SURFACES`, `heroScript`) |
| The final decisions | `nexus-design-mockup/stage/src/styles.ts` (`DEFAULTS`, `SELECTED`) |
| Logo icons and their generator | `interface/brand/`, `interface/brand/make_icons.py` |
| A partial NSH build to copy from | Worktree `.claude/worktrees/void`, branch `design/void`, see [below](#the-partial-build) |

To rebuild the page: `cd nexus-design-mockup/stage && npm install && npm run build`.

Page anchors, for the hosted or local page: `#selected` (the animated turn), `#notes`, `#window`,
`#light`, `#surfaces`. Each surface is `#surface-<id>`: `palette`, `settings`, `new`, `panel`, `files`, `git`,
`turn`, `sessions`, `diff`, `kids`, `approval`, `question`, `completion`, `model`, `alerts`,
`connect`, `phone`.

**Where the mock-up differs from the app on purpose.** Take these from the app, and not the mock-up:

- Sample sessions, projects and numbers are made up.
- Session rows: the mock-up's second line shows agent and branch. The app's shows its place (branch or
  project), which stays. Neither carries agent color.
- The rail: the mock-up has three icons. The app has two sections, so it gets two (step 7).
- Settings, starting context and the session panel: keep the app's rows and content, and restyle them.
- The glow, shimmer and pulse are canvas and CSS in the mock-up. Step 5 and step 4 say how to build them.

## Decisions

| Topic | Decision |
| --- | --- |
| Scope | NSH only. The TUI is unchanged, including its palette |
| Style | Void: true black, white ink, color only where it means something |
| Ground | Nothing behind or around the window. No glyphs, except on Connect |
| Window | Chrome band: header and sidebars share one surface around the page |
| User message | Full-width band, square, with a straight rail |
| Working | Shimmer on the word Working |
| Tool activity | Spine: one straight line beside the rows |
| Composer | Docked bar, edge to edge, its content in the reading column. Path, branch, worktree and context on their own line under the controls, as in the TUI |
| Context meter | The TUI's activity bar behavior along the dock's foot, as a line, slightly glowing, in the agent's color |
| Session row | Two lines |
| Waiting card | Key chips, in a floating card above the dock |
| Left toggle | The first control at the top left, and it doesn't move when the sidebar closes |
| macOS traffic lights | In the band, with a hidden title bar. The toggle sits right after them, and flush left in full screen |
| Right sidebar | Icon rail. The panel opens on demand and pushes the page |
| Selection | Soft pill, with no focus bar |
| Box rule | A border stops before a corner curves. A filled box needs no border |
| New session | Starting context on the timeline, at the top, composer docked below |
| Agent color | The composer's agent name and the bar. **No agent color in the session sidebar or the turn footer** |
| Fonts | Geist for interface text and prose. Monaspace Argon for code, paths and tool arguments |
| Themes | Dark and light both stay |
| Logo | Slash N, not bold, for icons only. NSH never draws it |
| Connect | Plain heading, with a field made only of `_/` |

## Tokens

**Do not edit `interface/theme.toml`.** The TUI embeds that file at build time
(`nexus-tui/src/theme.rs`), so a change there would change the TUI.

Put these values in a new overlay, `nexus-nsh/theme-void.toml`, using the same `[dark.surface]`
style of groups. `nexus-nsh/scripts/gen.py` loads `interface/theme.toml`, then merges the overlay
over it before generating. Glyphs, roles, agent colors and pins keep coming from the shared file.

`interface/check_theme.py` only sees the shared file today. Give it an optional `--overlay <path>`
that merges before checking, and add that check where the theme check runs: `scripts/nexus:264` and
`.github/workflows/ci.yml:58`.

| Token | Dark | Light |
| --- | --- | --- |
| `surface.canvas` | `#000000` | `#fbfbfa` |
| `surface.panel` | `#0e0e10` | `#efefed` |
| `surface.raised` | `#161618` | `#ffffff` |
| `surface.sidebar` | `#0a0a0b` | `#f1f1ef` |
| `text.primary` | `#f4f4f5` | `#0b0b0c` |
| `text.secondary` | `#a7a7ae` | `#4b4b50` |
| `text.muted` | `#85858d` | `#5f5f65` |
| `text.faint` | `#6e6e76` | `#83838a` |
| `border.default` | `#1c1c1f` | `#e2e2df` |
| `border.strong` | `#2e2e33` | `#cfcfcb` |
| `border.focus` | `#f4f4f5` | `#0b0b0c` |
| `interactive.accent` | `#f4f4f5` | `#0b0b0c` |
| `interactive.multi` | `#a78bfa` | `#6743ae` |
| `interactive.on_accent` | `#000000` | `#ffffff` |
| `interactive.focus` | `#f4f4f5` | `#0b0b0c` |
| `interactive.selection` | `#1a1a1d` | `#e7e7e4` |
| `interactive.hover` | `#111113` | `#ececea` |
| `status.success` | `#5ad58a` | `#146f36` |
| `status.warning` | `#e8c36a` | `#8a5d00` |
| `status.error` | `#f2707f` | `#c42b3d` |
| `status.info` | `#6aa8ff` | `#1f62c9` |
| `diff.add` | `#5ad58a` | `#146f36` |
| `diff.del` | `#f2707f` | `#c42b3d` |
| `diff.add_bg` | `#0c2015` | `#eef9f1` |
| `diff.del_bg` | `#2a0f14` | `#fbe3e6` |

These values passed `check_theme.py` (512 contrast checks) when applied to the full theme. The light
`status.success`, `diff.add` and `diff.add_bg` were adjusted to pass, so the mock-up's light values
for those three differ slightly. Use the table.

**Accent now equals primary text.** See [Open question](#open-question).

## Work order

Do steps 1 and 2 first, since everything else reads them. The rest are independent, and this order shows
results soonest.

| Step | What | Partial build | Size |
| --- | --- | --- | --- |
| 1 | Palette overlay | Redo | Small |
| 2 | Fonts and radius | Copy | Medium: the `font-mono` sweep |
| 3 | Box rule | Copy, then error inputs | Medium |
| 4 | User band, spine, shimmer | Copy | Small |
| 5 | Docked composer and activity bar | Copy, then move `LocationStrip` | Medium |
| 6 | Selection, session sidebar, turn footer | Copy, then delete `SelectBar` | Small |
| 7 | Icon rail and pushing panel | Copy, then check Git count | Medium |
| 8 | Project hover card | New | Small |
| 9 | Left toggle and macOS traffic lights | New | Medium: try it early, it's unproven |
| 10 | Headings | Copy | Small |
| 11 | Waiting card | Copy, then fix a test | Small |
| 12 | New session | Nothing to do | None |
| 13 | Logo, icons, Connect | Copy, then favicon step | Medium |
| 14 | Docs and tests | New | Medium |

`EDITOR_GIT_PLAN.md` is being built at the same time and touches `nexus-nsh/App.tsx`, `store.ts`,
`commands.ts`, `centre.tsx` and `interface/manifest.toml`. Rebase before steps 5 and 7, which overlap
the most. Its right panel and the icon rail in step 7 must share one layout rule.

### 1. Palette

- Add `nexus-nsh/theme-void.toml` and the merge in `scripts/gen.py`, as in [Tokens](#tokens).
- Extend `interface/check_theme.py` with `--overlay`. Its default behavior must stay the same.
- `npm run gen` rewrites `nexus-nsh/gen/theme.json` and `src/gen/theme.ts`, which are tracked. Commit them.
- **Done when:** `check_theme.py --overlay nexus-nsh/theme-void.toml` and `nsh check` pass,
  `git diff interface/theme.toml` is empty, and a TUI build looks the same as before.

### 2. Fonts and radius

- **Mock-up:** `--n-sans`, `--n-mono` and `--n-r*` at the top of `nexus.css`.
- **`nexus-nsh/scripts/gen.py`:** `RADIUS` becomes `radius: 10px`, `radius-card: 16px`.
  `FONTS` becomes `font-sans` Geist, and `font-mono` and `font-code` Monaspace Argon.
- **Files:** add `Geist-{Regular,Medium,SemiBold,Bold}.ttf` and `Geist-LICENSE.txt` to
  `nexus-nsh/fonts/` (the `geist@1.3.1` npm package has the TTFs, and the worktree has them).
  In `App.tsx` import those and drop Neon. Delete `MonaspaceNeon-*.otf`.
- **`font-mono` meant interface text until now,** so this is a real sweep. In
  `src/ui/timeline.tsx` remove `font-mono` from prose, user messages, verbs, labels and the
  footer. Keep it only on `r().arg`, `p.line().text` and `p.block.language`. Remove it from the
  composer's `Input`. Check other `src/ui/*.tsx` by eye.
- **Docs:** `nexus-nsh/README.md` lines on Monaspace Neon.

### 3. Box rule

**Mock-up:** `.n-edge` in `nexus.css`, and `#surface-approval` for a close look.

- Add `Edges` to `src/ui/kit.tsx`: four absolute `View`s inset by the box's radius. Set `left` and
  `leftWidth` for a colored rail.
- Remove `border border-border` from every rounded box in `src/ui/*.tsx`. A filled box keeps its
  fill and nothing else. Use `Edges` on tool cards and diagnostics in `timeline.tsx` and on the
  waiting card.
- Strip border classes from `Btn`'s `skin` in `kit.tsx`, and turn `selected:border-ring` into
  `selected:bg-selection`.
- Straight rules like `border-b`, `border-l` and `border-t` are fine. Keep `FOCUS` outlines, which
  are a focus state.
- Inputs with `border-error` still curve, and need another error signal.

### 4. User band, spine, shimmer

**Mock-up:** `UserMsg`, `ToolRow`, `Activity`, `Working` in `parts.tsx`; `#surface-turn`.

- **`src/ui/timeline.tsx`:**
  - In `msg-user`, drop `rounded-(--radius-card)` and make the rail
    `absolute left-0 top-0 bottom-0 w-0.5` with no rounding. Remove the rounding on the pinned band too.
  - In `ActivityItem`, replace the branch and tick with one `w-px bg-border-strong` line for every
    row, last included.
- **`src/ui/composer.tsx`:** `WorkingRow` uses a `Shimmer`. I didn't find a way to clip a gradient to
  text in NSH, so animate each letter's opacity with `animationName: "pulse"`, staggered. Static under
  `ui.reduced_motion`.

### 5. Docked composer and activity bar

**Mock-up:** `Composer`, `ActivityBar` in `parts.tsx`; `.n-comp--dock`, `.n-bar` in `nexus.css`;
`#notes` shows rest and running.

Bar behavior comes from the TUI's `draw_activity_line` (`nexus-tui/src/render.rs`). Read it, and
don't change it. At rest, the first `fraction` of the width is lit. While a turn runs, a soft-edged
pulse a fifth of the width glides on `travel * (1 - cos(2π * phase)) / 2`. The sweep takes about
three seconds. Under reduced motion it holds at phase 0.5.

In `src/ui/composer.tsx`:

- Split the root in two: a `TimelineColumn` for `WorkingRow`, `PendingStack` and completion, then a
  full-width `bg-sidebar` dock holding a second `TimelineColumn` and the bar. `safeArea().bottom`
  goes on the dock.
- Remove the card classes and the focus border.
- Keep `LocationStrip` on its own row under the controls, at every width, as in the TUI: path, branch and
  the linked-worktree icon on the left, context (`82k · 41%`) on the right. `composer.tsx` already has
  this row, so only remove the `w-20 h-1` meter from it, along with its `meterPct` and `meterColor` props. The row order in the dock is input, controls,
  strip, then the bar. It keeps `location-*`, `composer-meter` and `composer-usage` test IDs.
- `ActivityBar`: a 1 px `bg-border-strong` line with an absolute fill (`boxShadow 0 0 4px` for the
  glow), or a gradient pulse while busy. Get the color with `token("agent-<slot>")` from `agentSlot`.
- **Done when:** the numbers and the bar agree at 0%, 41% and 100%, with reduced motion, and at phone
  width.

### 6. Selection, session sidebar and turn footer

**Mock-up:** `.nx[data-select="pill"]`, `.n-row__l2 .n-ag`, `.n-foot .n-ag` in `nexus.css`; `#surface-sessions`,
`#surface-turn`.

- Remove the bars: completion rows in `composer.tsx`, `SelectBar` in `modal.tsx` (delete it and its
  callers: three in `overlays.tsx`, one in `settings.tsx`), `row-focus-bar` and the project bar in `sidebar.tsx`. Selected rows get
  `bg-selection rounded-(--radius)`.
- Headers: active tab and `new-session-tab` go from `bg-background` to `bg-selection`, in `header.tsx`.
- **No agent color in the session sidebar.** `src/ui/sidebar.tsx` doesn't use `agentColor` today, so
  there's nothing to remove. Keep it that way. If you add the agent name to the row as the mock-up
  shows, make it `T.meta` muted. The comment in `i6.test.tsx` that says "the agent in its colour" is out
  of date.

- **No agent color in the turn footer.** The real footer (`turn-footer` in `src/ui/timeline.tsx`) doesn't
  use `agentColor` today, so there's nothing to remove here either. Keep its text muted.

Agent color stays on the composer's name, and on the other places that use `agentColor` today:
`overlays.tsx`, the child card in `timeline.tsx`, `header.tsx` and `right.tsx`.

### 7. Icon rail and pushing panel

**Mock-up:** `Right` in `parts.tsx`; `#surface-panel`, and any shell for the closed state.

- **Rail contents:** Session and Git only. Those are the two right-sidebar sections in
  `manifest.toml`. Don't copy the mock-up's third Context icon unless you add a section. Show the
  count of changed files on Git.
- `rightIsOpen` in `src/commands.ts` defaults to `rightOpen() ?? false`.
- Add `RightRail` to `src/ui/right.tsx`, `w-11 bg-sidebar`. Mount it in `App.tsx` after
  `RightSidebar`, on non-narrow windows only. Icons run `sidebar.right.toggle`.
- Narrow keeps its drawer. The phone keeps its details button.
- The header's `toggle-right` stays, since `manifest.toml` requires that anchor. Don't change the
  manifest for this: the TUI reads it too.
- Agree the layout rule with the `EDITOR_GIT_PLAN.md` panel.

### 8. Project hover card on the collapsed rail

**Mock-up:** `#window-rail-hover`. **Reference:** `draw_rail_popup` in `nexus-tui/src/render.rs`. Read it,
and don't change it.

With the left sidebar closed, hovering a project on the rail shows its name and activity, as in the TUI.
`src/ui/sidebar.tsx` already has a `tooltip` on `rail-project-<id>`, so build on that. I haven't seen it
running. Match the TUI:

- **Content:** the project's name in bold, then two rows, zero counts included: `N attention` and
  `N unread` on top, `N running` and `N idle` on the bottom, in aligned columns. This mirrors the rail's
  counters above and below the initials. The wording is shorter than the TUI's, so change `details()` in
  `sidebar.tsx`, which also feeds the `accessibilityLabel`. The card itself needs a row of two `Text`s
  in place of its current list.
- **Color:** the tooltip makes the lines muted today. Use `text-multi`,
  `text-info`, `text-success` and `text-foreground`, in that order, which `tones` already holds.
- **Surface:** `bg-raised` with padding and no border. This follows the box rule in step 3.
- **Position:** beside the rail, level with the hovered project, on top of the page.
- **Keyboard:** the TUI also shows it when the project has focus in the rail. Do the same, and keep the
  existing `accessibilityLabel`.
- **Timing:** check that NSH's tooltip delay feels immediate. If it can't, draw the card from the rail's
  hover state.
- **Touch:** no hover, so nothing changes on touch screens.
- **Tests:** add one for the wording and the two rows. Nothing checks the old text today.

### 9. Left toggle and macOS traffic lights

**Mock-up:** `#window`, with `#window-web`, `#window-web-closed`, `#window-hidden` and
`#window-hidden-closed`.

- **Web:** `src/ui/header.tsx` already puts `toggle-left` first in the band, flush left. Keep it there
  when the sidebar closes.
- **macOS (decided: lights in the band, `#window-hidden`):**
  - Call `Window.setTitleBar({ style: "hidden", height })` from `nsh/desktop`, with the band's height.
  - Pad the band's left cell by the traffic lights' width from `titleBar()`. It reports zero in full
    screen, so the toggle moves flush left there.
  - Keep the toggle in the same spot with the sidebar closed (`#window-hidden-closed`): the rail starts
    below the band.
  - Mark the band with `windowDrag`, or the window can't be dragged any more. Its controls must still
    take clicks.
  - I read this in NSH's docs and source, and haven't run it. Nexus doesn't use `nsh/desktop` yet, so
    try it early on the desktop build, and check `titleBar()`'s field names there.
- **iOS and the phone:** no change.

### 10. Headings

**Mock-up:** `.n-label`.

Replace `text-[11px] leading-4 font-semibold uppercase tracking-[0.06em]` with
`text-[12.5px] leading-4 font-medium`, across `src/ui/*.tsx`.

### 11. Waiting card

**Mock-up:** `Approval`, `Question`; `#surface-approval`, `#surface-question`.

The card is `relative` with `Edges left="bg-primary" leftWidth={2}`, not a full-height `w-[3px]` rail
in an `overflow-hidden` box. Drop the 3 px `View`, and either keep the `waiting-rail` test ID on the
Edges rail or update `i8.test.tsx:72`. Key chips stay, with fills in place of borders. A multi-select
question uses `bg-multi`.

### 12. New session

**Mock-up:** `StartContext`, `NewMain`; `#surface-new`.

Already on the timeline in `src/ui/newsession.tsx`, so don't rebuild it. Only the composer changes,
through step 5. It inherits the palette, and its section labels follow step 2.

### 13. Logo, icons, Connect

**Mock-up:** `SlashField`, `Connect`; `#surface-connect`, `#notes`.

- **Logo out of the UI:** NSH already draws no logo, so there is nothing to remove. Keep it that way.
- **Icons:**
  - Set `"icon": "../interface/brand/nexus-1024.png"` in `nexus-nsh/nsh.json`.
  - **Not yet verified:** that `nsh package` accepts a path outside the app folder, on web, macOS
    and iOS.
  - **Known gap:** `nsh-cli/src/web.rs` makes its favicon by downscaling the one icon, so the
    pixel-tuned 16 and 32 px files will not be used. Add a post-build step that replaces
    `icons/32*.png`, or use `favicon.ico`.
  - The 16 px icon is at its limit: it reads as marks more than as an N. Look at it in a real tab.
- **Connect (`src/ui/connect.tsx`, native only):**
  - Add `slashField(cols, rows)`, a pure function returning rows of `_/ ` in soft clusters, clear in
    the middle. Draw it in an absolute, non-interactive layer behind the `Scroll`, in `font-mono`
    `text-border-strong`, with test ID `connect-field`.
  - Heading: `Connect to Core`.

### 14. Docs and tests, last

- **`docs/porting-ledger.md`:** add one row, at the user's request: NSH uses Void (palette from the overlay,
  rounded boxes whose borders stop before the curve, docked composer, soft pill, sentence-case headings,
  spine, icon rail, Geist, hidden macOS title bar, no logo, no agent color in the turn footer, shorter
  hover card wording), while the TUI keeps its amber palette and layout.
- **`interface/design-language.md`:** it describes the TUI, so leave its rules alone. Add one line
  saying NSH follows this plan's design instead, and `theme-void.toml` overrides `theme.toml` for NSH.
- **`nexus-nsh/README.md`, `docs/development.md`:** composer meter text (`╱`), fonts, the overlay.
- **`PLAN.md`:** update when this lands. Delete `nexus-design-mockup/` when its README says to.

## Testing

Use a scratch `$NEXUS10_HOME`, and never the real home.

```sh
cd nexus-nsh
npm run gen && nsh check
nsh test --target web --browser "$(find ~/Library/Caches/ms-playwright/chromium-1248 -type f -name 'Google Chrome for Testing' | head -1)"
```

**The baseline is not clean.** On unmodified `main` at `89fadf93`, 13 of 122 tests fail:

- `App.test.tsx`: starting context items, the two daemon-restart `history` tests, session info
  totals, palette, pre-session picker, both tool output detail cap tests
- `i6.test.tsx`: uppercase project header
- `i8.test.tsx`: both alerts tests
- `parity.test.tsx`: child result
- `toolmedia.test.tsx`: tool images

That baseline run was cut off with no summary line, after every test had printed a result.

The partial build added these 5, which are likely yours to fix or update as you change the design:

- `App.test.tsx › session info totals › says when Core has not reported usage`
- `App.test.tsx › turn summaries › unknown usage reads unknown…`
- `i6.test.tsx › right sidebar › Git shows the branch…`
- `i6.test.tsx › right sidebar › Session info and Context sections…`
- `i8.test.tsx › waiting card › an approval shows the title, timer, table…`

The first four probably fail because the right panel now starts closed, so open it first in those
tests. I didn't confirm that. The fifth looks for `waiting-rail`. `i6`'s uppercase test also has the
wrong name now.

**By eye**, with the mock-up beside you:

- On a scripted Core: `e2e/scripted_core.py` and `e2e/seed.mjs` via `e2e/up.sh`, then
  `node e2e/shots.mjs`. `up.sh` fails if ports 4811, 4812, 4820 or 4821 are taken.
- Dark and light, at 390, 1024 and 1440 wide, and with reduced motion.
- On the macOS build: the lights line up with the band, the window drags from the band, and the
  toggle moves in and out of full screen correctly, with the sidebar open and closed.
- `nexus check` before you finish.

**Confirm the TUI didn't change:** `git diff --stat -- nexus-tui interface/theme.toml` is empty, and
`cargo test -q` in `nexus-tui` gives the same result as before you started.

## Open question

**Accent equals primary text.** In NSH, links and active states now look like plain text. Decide per
element: links probably need an underline. There are about 20 `bg-primary` and `text-primary` uses in
`src/ui/*.tsx`. Filled buttons are fine as white. Check the text ones, like the waiting `◆`, the sidebar's `+`
and `Show all` in `timeline.tsx`. You can decide this at step 14.

## The partial build

Worktree `.claude/worktrees/void`, branch `design/void`, based on `89fadf93`, all uncommitted.
Copy from it or delete it. It isn't meant to be merged as is.

```sh
# From the repo root. All of src/ui applied cleanly to the main checkout on 2026-11-01 (--check only).
git -C .claude/worktrees/void diff -- nexus-nsh/src/ui nexus-nsh/src/commands.ts | git apply -

# App.tsx conflicts with the editor work, so make its two changes by hand:
#   swap the Neon font imports for Geist, and mount <RightRail /> after <RightSidebar />.
git -C .claude/worktrees/void diff -- nexus-nsh/App.tsx

# Copy these too:
cp .claude/worktrees/void/nexus-nsh/fonts/Geist-* nexus-nsh/fonts/
git -C .claude/worktrees/void diff -- nexus-nsh/scripts/gen.py nexus-nsh/nsh.json | git apply -
```

Then build the overlay from step 1, or the app keeps the amber palette, and rerun the check if the editor
work has moved on. Delete the worktree when you're done:
`git worktree remove --force .claude/worktrees/void && git branch -D design/void`.

**Do not copy these from it:** its edits to `interface/theme.toml`, which would change the TUI, and
its notes in `docs/porting-ledger.md`, `interface/design-language.md` and `PLAN.md`, which say the
TUI takes the palette. Its `nexus-tui/` changes have been reverted. With `theme.toml` reverted too,
NSH there would lose its colors until the overlay from step 1 exists.

| Step | State in the worktree |
| --- | --- |
| 1 | Tokens are in `interface/theme.toml`, the wrong place. Overlay not started |
| 2 | Done. Neon font files deleted there, so delete them here too |
| 3 | Done except error inputs |
| 4 | Done |
| 5 | Done, except that `LocationStrip` is inline on wide windows. Move it back to its own row |
| 6 | Done, but `SelectBar` is a leftover no-op |
| 7 | Done. `changes()` in `RightRail` guesses `gitStatus` field names, so check it |
| 8 | Not started |
| 9 | Not started |
| 10 | Done |
| 11 | Done. `i8.test.tsx` not updated |
| 12 | Nothing needed |
| 13 | Connect and `nsh.json` done, neither seen running (Connect is native only). Favicon step not started |
| 14 | Short notes only, wrong on the TUI, so don't copy them |

NSH type check passes there. I looked at the web build on a scripted Core, in dark and light, for
session, question and panel. I didn't see: a live approval card, the running pulse on screen, phone
sizes, native builds or Connect.
