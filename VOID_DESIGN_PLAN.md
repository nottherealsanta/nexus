# Plan: the Void design for NSH, second pass

Status: the first pass landed on 2026-11-01 and the second pass landed on 2026-11-01. This page replaces the first pass's plan and records the design NSH now has; the steps below are the record of what was applied.

Scope: NSH only, on web, desktop and iOS. **The TUI stays as it is**, so don't edit `nexus-tui/` or `interface/theme.toml`. Palette values live in `nexus-nsh/theme-void.toml`.

## The problem

The first pass got the palette and the big structure right: black canvas, chrome band, docked composer, icon rail. It looks unfinished for four other reasons, all visible in screenshots of NSH web at 390, 1024 and 1440 wide, dark and light.

1. **Things are broken at the edges.** Traffic lights sit on top of `Back`. Model, provider and effort overprint each other. The `New session` label sits 4 px high in its tab. A `2 other sessions` card covers the prose.
2. **There is no shared grid.** Text in the reading column starts at four different x positions and ends at four others. The sidebar has three left edges. Header toggles sit 3 px off the rails below them.
3. **TUI notation survived into a graphical UI.** `[18]`, `( )`, `[x]`, `▸`, `✓ Project`, an idle `·` on every tab, and a grey dot beside every tool.
4. **Boxes are drawn three different ways.** Some have broken hairlines with a floating rail, some a rail bent around the corner, and the band is square. Search and segmented controls use the card radius, so they read as pills beside 10 px controls.

The second pass fixes these with the rules below, then applies them surface by surface.

## Rules

Every step follows from these. If a case isn't covered in a step, apply the rule.

| Rule | Meaning |
| --- | --- |
| **One text edge** | In the reading column, boxes start at the column edge, and all text starts 16 px in: prose, band, card contents, activity labels, turn footer, composer input, controls, location strip |
| **One right edge** | All right-aligned meta ends 16 px inside the column's right side, including durations, band stamp, footer and send button |
| **One sidebar edge** | Everything in the left sidebar starts at 16 px: search text, project name, chips, session titles, group headings, Settings icon |
| **Chrome lines up** | A header toggle's centre matches the centre of the rail below it, on both sides |
| **Filled boxes have no border** | Every box is filled, so no box gets a hairline. Straight dividers stay |
| **Status rail** | A card that carries status gets one inset rail: 3 px wide, fully rounded, 6 px from the left, 12 px from top and bottom. Only the square user band keeps its flush rail |
| **Radius** | 16 px for cards and dialogs. 10 px for every control, input, row and chip. Full rounding only for dots and rails |
| **Selection is a pill** | Selected means `bg-selection`, the same everywhere. No checkmarks, carets, or bold-only states |
| **No terminal notation** | Draw controls and numbers as UI. Glyphs are fine where they mean status: `◆`, `✕`, `◐` |
| **Type** | Prose 14/22. Interface 13/20. Meta 12/16. Code beside Geist is 12.5/20, because Monaspace Argon at 13 looks a size larger than Geist at 13 |
| **Space scale** | 4, 8, 12, 16, 24. Two adjacent boxes are always at least 8 px apart |

### The column

```text
column edge   text edge                                     right edge   column end
|             |                                                      |   |
|<--- 16 ---->|                                                      |<16>|

box           Run the full scenario review                  #1 · 11:17
              > 5 thoughts, 1 command                       ✕ 2 failed  0.2s
              ^ chevron hangs in the inset, label lands on the text edge
              The notes file is missing, so I stopped.
              m · 1 tool · 480 in · 18 out · 11:17          (left, at the text edge)

dock          Type a message…
              Build   DeepSeek V4.1 Flash   default              mic  send
              ~/repos/nexus  ⌥ main                          5.4K · 0.5%
```

## Work order

Do step 1 first, so the app stops looking broken, then step 2, since the later steps sit on its grid. The rest are independent.

| Step | What | Size |
| --- | --- | --- |
| 1 | Breakage | Small |
| 2 | Grid and shared kit | Medium |
| 3 | Chrome band and rails | Small |
| 4 | Left sidebar | Medium |
| 5 | Starting context | Medium |
| 6 | Timeline | Medium |
| 7 | Waiting card | Medium |
| 8 | Dock | Small |
| 9 | Files and file panel | Medium |
| 10 | Session panel | Small |
| 11 | Settings and dialogs | Small |
| 12 | Light theme | Small |
| 13 | Docs and tests | Small |

The checkout has unrelated staged work in `composer.tsx` and `commands.ts`, among others. Work on a branch and don't sweep it into these commits.

### 1. Breakage

Each of these has a confirmed cause, except where marked.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Traffic lights cover `Back` when a file opens in a narrow window | `FilePanel` goes full-window at `top: 0`, with no room for the lights | Below `PANEL_WIDE`, pad the panel header left by `titleBar().left + 8` on macOS, make it `min-h-11`, and mark it `windowDrag` |
| Most lines of a file look blank once scrolled sideways | Not a paint bug: `softWrap` is off, so long lines scroll the whole editor sideways and short lines leave the view | Set `softWrap` on `Editor` in `editor.tsx`, for reading and editing. Leave `DiffView` as it is |
| `opencode` and `default` overprint in a narrow column | `Selector`'s `Button` shrinks, but its `Text`s have no `min-w-0`, so they paint outside it | In `Selector`, give the label `min-w-0 shrink` and the sub-label `min-w-0 shrink-[2]`. Give the effort selector `shrink-0`. Hide the provider when the column is under 560 px, using `onLayout` and not the window width |
| `New session` label sits 4 px high in its tab (measured: text top 8, button 8 to 36) | `new-session-tab` has no `justify-center` | Add `flex-row items-center justify-center` |
| `2 other sessions are waiting` covers the prose | It's a 66 px floating card in `WaitingCard` | Move it into the dock (step 8) |
| `Working · 2m 01s · ↓0` runs under `waiting on you` | `WorkingRow` ignores the waiting state | Hide `WorkingRow` while the viewed session has a waiting item |
| Waiting card touches the band above it | No gap between them | Give the waiting slot `mt-2` |
| Pinned band sits about 64 px right of the column it belongs to | `StickyBar`'s wrapper still has `items-center`, from before the reading column moved left, so it centres the column | Change it to `items-start` |

**Done when:** at 700 px wide with a long model name nothing overlaps, and `AGENTS.md` reads top to bottom with no sideways scroll.

### 2. Grid and shared kit

**`src/ui/reading-column.tsx`:**

- Delete `ACTIVITY_INDENT` and `ACTIVITY_CONTROL_INSET`. Keep `CARD_INSET` (`px-4`) and `TEXT_INDENT` (`pl-4`), and add `TEXT_INSET = "px-4"` for full rows.
- Add `ACTIVITY_CHEVRON = "w-4 shrink-0"`, the slot that hangs a chevron inside the inset. The row itself has no left padding, so its label lands on the text edge. Give hover fills `-mx-1 px-1`, so they overhang without moving the text.

**`src/ui/kit.tsx`:**

- Delete `Edges`. Its callers are the waiting card in `composer.tsx` and three rows in `timeline.tsx`. Filled boxes keep their fill.
- Add `StatusRail`, following the rule above, with `color` and `testID` props. Replace `Rail` in `timeline.tsx` with it. The parent is `relative`.
- Change `T.mono` to `font-mono text-[12.5px] leading-5 text-foreground-secondary`, and use it wherever code sits beside Geist. Sweep `font-mono text-[13px]`, and leave the editor's own font size alone.
- Add `Seg`, moved out of `settings.tsx`, as the one segmented control. The well is `bg-muted p-0.5 rounded-(--radius)`, each option `rounded-[8px]`, and the selected option `bg-selection text-foreground font-medium`.
- Replace `rounded-(--radius-card)` with `rounded-(--radius)` on anything that isn't a card, a dialog or a menu. Known cases are `session-search`, `Seg` and the folded band. The folded band goes square, to match the open one.

### 3. Chrome band and rails

**Mock-up reference for the band:** `#window` in the mock-up. These details override it.

- **Tabs (`header.tsx`):**
  - Put `+` straight after the last tab, inside the scroll content, not at the far right. It shouldn't move when the right panel opens.
  - Show no glyph for an idle tab, and keep glyphs for running, waiting, failed and unread. Keep the idle state in the accessibility label.
  - Use the `x` icon in place of the `×` text. Add it to `src/icons` if it's missing.
- **Section tabs (`SidebarTabs`):** add `bg-selection` to the selected tab, on top of the tint. Today only the tint marks it. Put a `w-px h-4 bg-border mx-1` divider between `toggle-left` and the tabs.
- **Alignment:** today the left toggle's centre is at 24 and the rail's at 21, and on the right they're at `W-24` and `W-21`.
  - Set both rails to `w-12` and centre the toggles at 24 from each side.
  - Update `RAIL = 42` in `editor.tsx` to 48.
- **Project rail (`SessionRail`):** the counters of neighbouring projects run together, and the pill touches the rail's sides. Use `size-10` cells with `gap-2` in the `w-12` rail.
- **Right rail:**
  - Mark only the section the panel is showing. Today `rail-git` is never selected.
  - Git opens the panel and scrolls to its section. Today both icons only toggle.
  - Show the change count as a small `bg-border-strong` badge, not bare text on the icon.
- **`Header` band:** keep the 44 px height. The band and sidebars stay one surface.

### 4. Left sidebar

**Search:** `h-8 rounded-(--radius) bg-hover`, not the black well. Put a 14 px `search` icon inside it, on the sidebar edge, with the placeholder after it. The sidebar has no other search affordance once text is typed, so the icon stays.

**Project row and chips:**

- Drop `✓ ` from `Chip`, because it moves the label when toggled. Selected is `bg-selection text-foreground font-medium`, as on the phone.
- Keep the labels and test IDs. Web says `Global` where the phone says `All projects`, which is a known difference to settle in [Open questions](#open-questions).

**Session rows (`SessionRow`):**

- Remove the 22 px status gutter, which pushes every title away from the sidebar edge. Titles start on the sidebar edge.
- Status moves to the right, in front of the time: `◆ 2m`, `◐ now`.
- Under a project heading, stop repeating the project name. `place()` returns `projectName` when `scopeAll()` is on, so each group reads `Nexus / Nexus`. Show the branch there, the same as the project scope.
- When a row has nothing on its second line, it's one line, `h-8`. Otherwise it stays `h-12`.
- Keep titles `font-medium` in every state, and let the pill carry selection, since bold makes the text reflow against the time.

**Group headings:** keep them as they are, but `pt-3 pb-1`, on the same edge.

**Projects tab:**

- Shorten paths with `~`, using the helper `LocationStrip` has inline. Move it to `kit.tsx`, and use it in both places.
- Truncate at the start of the path with `midTruncate` only after shortening.
- Replace `1 · 2m` with `1 session · 2m`.
- Put the `Projects` heading on the sidebar edge. Give `Refresh` in `ProjectMcpPanel` a ghost button look with `text-muted-foreground`.

### 5. Starting context

This is the first thing every session shows, and it's the most visible flaw. Make it a compact ledger: names on the left and token counts in a column on the right.

```text
System prompt                                                  ~1.6K
AGENTS.md                                                       ~946
Tools  18                                                      ~3.5K
  read       grep       glob        ls         bash       bash_output
  bash_kill  edit       patch       write      web_search web_fetch
Skills  2
  nexus-ios-device  project         stageshow  project
MCP  1
  browser-use  16 tools
────────────────────────────────────────────────────────────────────
Total                                          ~5.4K · 0.5% of window
```

In `Section` and `ContextSections` in `newsession.tsx`:

- **Header row:** title, then a muted count with no brackets (`Tools 18`), then a `flex-1` gap, then tokens right-aligned with `tabular-nums`. A section's `meta` (`not found`, `none`) sits in the token column, muted.
- **Tools:**
  - Delete the dot for items that are on. Off stays `line-through`, with `opacity-60`.
  - Replace `w-1/3` columns, which truncate to `bash_k…`, with `flex-row flex-wrap gap-x-4` and fixed `w-[196px]` cells. 196 px fits `change_session_directory` at 12.5 px, and as many columns fit as the width allows.
  - Under 420 px of card width, use natural-width items, so nothing truncates on a phone.
  - Remove the `pl-5` indent in favour of `pl-3`.
- **Skills and MCP:**
  - Use the same cell as tools, on one line: `name` then muted `project` or `16 tools`. That removes the double-height rows.
  - Keep them as `role="switch"` buttons with the hover pill. A click toggles, so show off the same way as tools.
  - A row with an error or a `new session` lock takes the full width, on two lines.
  - Keep `ctx-*` test IDs and accessibility labels with `on` and `off`.
- **Total:** a `h-px bg-border` divider above it. `Total` is capitalised, and the figure is compacted with `contextTokens`, so it matches the location strip. `share()` stays, right-aligned.
- **Surface:** `bg-card`, `rounded-(--radius-card)`, `px-4 py-3`, and `my-3` in place of `my-5`, so the band follows closer.

The figures stay estimates with their tooltips, and nothing is hidden.

### 6. Timeline

**User band (`UserBand`):**

- Stamp and text share a first baseline: `flex-row items-start`, and the stamp is `pt-[3px]`, with no extra height. Today hover icons make the row 32 px, so one-line bands are lopsided and differ in height.
- Take the hover icons out of the flow: `absolute right-3 top-2`, over a small `bg-raised` backing, and hide the stamp while they show.
- On touch, where they're always visible and squeeze the message to four lines, move them with the stamp to their own row below the text, right-aligned.

**Activity rows:** chevron in `ACTIVITY_CHEVRON`, label on the text edge, as in [The column](#the-column). Failures and duration on the right edge. Hide durations under 0.1 s. The spine stays at the chevron's centre, `left-[7px]`, and its rows start on the text edge.

**Cards (`child`, `msg-diag`, handoff):**

- Replace `Edges` and `Rail` with `StatusRail`, and remove `overflow-hidden`, which bends the child card's rail round the corner.
- `diagnostic` is a heading the user doesn't need. Show the message as the first line, in `T.sec`, and move `label` to the accessibility label. Merge consecutive identical diagnostics into one with `×2`. Two `interrupted by the user` cards are one fact.

**Turn footer:** left-aligned on the text edge, not right-aligned. Right-aligned, it wraps to a ragged block in a narrow column. Change `items-end` and `justify-end`. Keep the wording, including `+ unknown`, since unknown usage must never read as zero.

**Older messages:** make `Load older messages` a ghost button on the text edge and drop `Older messages are not loaded`, because the button says it. Keep the `timeline-older` test ID on the row.

**Background shell notice:** clip to one line with an expand control. Today it's cut mid-line where the dock starts. Show the command in `T.mono`, without the `→ Background Shell Done:` prefix text in raw form: `Background shell finished` then the command.

**Pinned band:** with step 1's fix, it matches the user band's left and right edges. Give it the same flush rail and fill as the band, in place of the `border-b`, so it reads as the same object pinned.

### 7. Waiting card

It floats above the dock and is the most important thing on screen while it's open. It should look deliberate.

```text
┃ ◆ Allow bash?                               1 of 2  ⌘]  next
┃   waiting on you · 0:02
┃   ┌ command  ls -la src/ && echo done
┃   └ timeout  20
┃
┃   [pill: Allow once          ⏎ ]
┃    Allow for session        ⌥⏎
┃    Deny                     ⌘⌫
┃    Deny with a note        ⇧⌘⌫
```

- **Surface:** `bg-raised`, one step above other cards, `rounded-(--radius-card)`, no border, with `StatusRail` carrying `testID="waiting-rail"`, so `i8.test.tsx:72` still passes. Keep `bg-multi` for a multi-select question.
- **Text edge:** `pl-4 pr-3 py-3`. Everything inside starts at the column's text edge.
- **Choices:**
  - Remove `▸`. The current choice is `bg-selection`, the soft pill.
  - Put the label first and the key chip on the right, so labels line up and the shortcut stops being the first thing read.
  - The chip is natural width, `bg-muted`, `text-xs font-medium`, not `w-16 font-bold`.
  - Keep `font-semibold` off `Allow once`, as the pill now does that job.
- **Question options:** replace `( )`, `(•)`, `[ ]` and `[x]` with drawn marks. Radio is a 14 px ring with a filled centre. Checkbox is a 14 px `rounded-[4px]` box with a check icon. Selected rows get `bg-selection`.
- **Tool table:** `bg-background` stays for code, with `rounded-(--radius)`. Labels are `T.meta`, values are `T.mono`.
- **Height:** cap at 60% of the centre column, and scroll inside it, so the timeline never disappears on a short window.
- **Status text:** `waiting on you · 0:02` becomes `Waiting on you · 0:02`, because the rest of the UI is in sentence case.

### 8. Dock

- **Edges:** remove `px-1` from `LocationStrip` and the composer's inner padding, and set the dock's content to `TEXT_INSET`. Input, agent name and path then line up with the prose. `Selector` with `alignStart` already has `pl-0`.
- **Other sessions waiting:** a slim `h-8` row at the top of the dock, in place of the card: `◆  2 other sessions waiting` on the left, and a ghost `Go to` on the right. Keep the `waiting-go-to` test ID. Divider below it.
- **Mic and send:** the same size and treatment at every width. Today narrow gets a `size-12 bg-raised` mic beside a `size-11` send. Use `size-8` ghost for mic and `size-8` filled for send on desktop, and `size-11` for both on narrow.
- **Foot:** add `pb-1.5` to the strip, which now sits against the window's bottom edge.
- **Activity bar:** on macOS, the bar's ends vanish in the window's rounded corner. Inset it 10 px on macOS only. Make the lit fill 2 px with the track still 1 px, or it can't be seen at low percentages.
- **Strip:** use `T.mono`'s 12.5 px for path and branch, and give context `tabular-nums`. Fix the `midTruncate` limits by measuring the row with `onLayout`: 54 characters overflows in a narrow column, and truncates needlessly in a wide one.

### 9. Files and file panel

**`FilesTab` in `sidebar.tsx`.** Ignored rows use `T.meta` and others `T.sec`, so sizes and row heights jump around, and the selected row is a full-bleed square.

- Every row is `h-7 rounded-(--radius) px-2 flex-row items-center gap-1.5`, with the list inset by 8 px, 13/20 at all times.
- A 14 px chevron slot, drawn with `chevron-right`, not `▸ `. Files leave the slot empty, so names line up.
- Ignored files use `text-faint`, at the same size.
- Git status sits on the right, in a fixed `w-4` column: `M` in `text-warning`, `A` in `text-success`, `D` in `text-error`, `?` muted. Folders with changes get a 6 px dot in place of a letter, because `M` on a folder reads as a file state.
- The selected file is `bg-selection`.
- The parent row `← path` becomes a compact breadcrumb in `T.meta`, on the sidebar edge.

**`FilePanel` in `editor.tsx`:**

- **Header:** the same `bg-sidebar` band as the app, `min-h-11`, using an `arrow-left` icon button in place of the text `Back`, then the path in `T.mono`. `Edit` becomes ghost, and only `Save` is filled.
- **Width:** 55% of the space leaves the timeline too narrow at 1440. Use `min(55%, 760px)`, so the timeline keeps at least 480 px, and make the divider draggable, using the same pattern as `left-sidebar-resize`. Below `PANEL_WIDE` it stays full-window.
- **Find bar:** icon buttons for previous, next and close, in place of three text buttons. `Aa` stays as a `Seg`-style toggle.
- **Gutter:** the editor's current line highlight stays. Remove the `shadow-2xl`, since the panel is a column and not a layer.

The composer's narrow-column behaviour is handled by steps 1 and 5.

### 10. Session panel

In `right.tsx`:

- **`Field`:** label and value share a baseline: both 13/20, with `pt-0.5` removed from the label, and label width `w-20`. `Agent` uses the same size as the rest. In your screenshot it's a size larger than its neighbours.
- **Paths:** one line, with `~` and middle truncation, and the full path in a tooltip. Today `Directory` wraps mid-word: `…/e2e/ws/n` / `exus-demo`.
- **Id:** the copy button goes to `size-5`, so that row is the same height as the others.
- **Git:** `Error: workspace is not a Git repository` becomes a muted `Not a Git repository`. Keep other errors verbatim.
- **Kernel note:** remove `Kernel state is not reported by Core yet.`, which is a note to developers.
- **Usage:** `2 requests reported no usage (unknown, not zero)` stays.
- **Sections:** `pt-5` between groups, and a `h-px bg-border` divider above `Git status`.

### 11. Settings and dialogs

**`settings.tsx`:**

- `Seg` comes from `kit.tsx`, so a selected option finally looks selected. Show options capitalised (`System`, `Light`, `Dark`), keeping stored values lowercase.
- Remove the repeated `Not editable in this NSH increment yet.` Rows with no control yet (`Density`, `Font`, `Font size`) show their value muted, with that sentence in a tooltip.
- Remove the `Appearance` heading inside the page, because the nav already says it. Keep headings on pages with several groups.
- Give the settings nav's chevrons a purpose or remove them. They appear on `Providers` and `Agents` only, and all the entries behave the same.
- Rows use the shared 12 px gap, and controls align to the right edge. Check inputs at `bg-background`: on the `raised` dialog they should stay wells, with `rounded-(--radius)`.

**Dialogs (`modal.tsx`, `overlays.tsx`):**

- Drop the top-right `esc` chip where a footer already says `esc close`. Keep one, in the footer.
- Scrim: raise `SCRIM` in `modal.tsx` from `bg-black/50` to `bg-black/65`, so the sidebar and timeline stop competing with the dialog. Today session titles stay bright behind the palette. Check it in light, and lower it there if it looks heavy.
- The palette's list and footer keep their shape.

### 12. Light theme

Light inherits everything above. Two values need attention in `theme-void.toml`:

- The user band is `raised`, `#ffffff`, on a `#fbfbfa` canvas, so it nearly disappears. Add `surface.band`: dark `#161618`, light `#f0f0ee`. Map it in `scripts/gen.py`, and use `bg-band` in `UserBand`, the folded band and the pinned band.
- Check `bg-hover` on the search field against `#f1f1ef` chrome. If it doesn't read as a field, use `bg-background` in light only, through a new `input` token.

Run `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml` after both.

### 13. Docs and tests

- **`docs/porting-ledger.md`:** edit the existing Void row. Replace the hairline wording with filled boxes and inset status rails, and add left-aligned turn footer, soft wrap in the editor, and status on the right in session rows.
- **`interface/design-language.md`:** still points here, so nothing to change unless the file is renamed.
- **`PLAN.md`:** update the Void entry when this lands.
- **Tests:**
  - `App.test.tsx` still presses `id:context-fold`, and `e2e/shots.mjs` does too, but no such control exists. Fix both with the [fold decision](#open-questions).
  - Update `timeline-layout.test.tsx` and `timeline-visual-parity.test.tsx` for the new insets.
  - Add tests for `Selector` at a narrow width, `softWrap` on the editor, `WorkingRow` hidden while waiting, and a one-line session row.

## What stays

These were decided and hold up in the screenshots, so don't reopen them.

- True black, white ink, and color only where it means something.
- The chrome band, with traffic lights in it on macOS and `toggle-left` first.
- The square full-width user band with a flush rail.
- Reading column on the left, not centred. Maximum 880 px.
- Docked composer, with the location strip on its own row and the activity bar at its foot.
- Shimmer on `Working`, and the spine beside tool rows.
- Icon rail, with a panel that pushes the page and starts closed.
- Agent color on the composer's name and the bar, not in the session sidebar or turn footer.
- Geist and Monaspace Argon. No logo in the UI.
- The starting context shown on the timeline, at the top, for new and running sessions.
- The mock-up (`nexus-design-mockup/stage`) as a reference for the look only. Where it differs from this page, this page wins.

The one reversal is the box rule. "A border stops before a corner curves" made filled cards look damaged, so it's gone, and "a filled box needs no border" now covers every box.

## Testing

Use a scratch `$NEXUS10_HOME`, never the real home. `e2e/up.sh` fails when ports 4811, 4812, 4820 or 4821 are taken, and they often are, so start the stack on its own ports:

```sh
cd nexus-nsh
npm run check
npm run build

S=$(mktemp -d); mkdir -p "$S/home" "$S/ws"; chmod 700 "$S/home"
(cd ../nexus-core && NEXUS10_HOME="$S/home" NEXUS_PORT=4832 uv run python ../nexus-nsh/e2e/scripted_core.py) &
sleep 3   # wait for daemon.json
node e2e/seed.mjs "$S/home" "$S/ws/nexus-demo" "$S/ws/docs-site" > "$S/ids.json"
NEXUS10_HOME="$S/home" NEXUS_NSH_NO_START=1 node scripts/serve.mjs --port 4830 &
```

Sessions open scoped to one project, so press `chip-global` before looking for a session row. `new-session` matches two elements, so use `.first()`.

**By eye,** dark and light, at 390, 1024 and 1440 wide, and with reduced motion:

| Surface | Look for |
| --- | --- |
| New session, session | Ledger columns line up, and there's one text edge from the card down to the location strip |
| 1440 with the file panel open | Timeline still readable, nothing overlaps in the dock |
| 1024 with a file open | Full-window panel, `AGENTS.md` wraps |
| Approval, question | Pill on the current choice, chips on the right, no terminal marks |
| Sidebar: global, project, files, projects, closed rail | One left edge, no repeated names, even row heights |
| Settings, palette | Selected segments are visible, scrim is dark enough |
| macOS build | Lights clear of `Back`, band and panel header drag the window, toggles line up with rails |

Measure alignment rather than judging it. In a Playwright session, compare `boundingBox()` of `toggle-left` against `rail-search` with the left sidebar closed, and of `toggle-right` against `rail-session`. The prose, `composer-agent` and `location-directory` should all share a left edge, within 1 px.

**Confirm the TUI didn't change:** `git diff --stat -- nexus-tui interface/theme.toml` shows nothing from this work, and `cargo test -q` gives the same result as before you started. The checkout has unrelated `nexus-tui/` changes staged, so compare against those.

Note `nsh test` and `nexus check` results before starting. The app tests have known failures, including the `context-fold` ones.

## Open questions

Each has a default, so none of them block the work.

1. **Fold the starting context once a session has messages?** The card takes about 300 px at the top of every session, and step 5 only shrinks it. The fold control is gone, but `foldedMeta` and the `open` field on the row remain, and tests still press `context-fold`. **Default: leave it open**, since "keep the context visible" is a core rule, and delete the leftovers. The alternative is to fold it to `Context · 5 items · ~5.4K tokens` after the first message, still in place and in order, and restore the control.
2. **`Global` or `All projects`?** Web and desktop say `Global` while the heading above says `All projects`, and the phone says `All projects`. **Settled: `All projects` everywhere**, and the test ID is `chip-all-projects`.
3. **Accent equals primary text,** so links and active states look like plain text. **Default: underline links in prose,** and leave controls as they are.
