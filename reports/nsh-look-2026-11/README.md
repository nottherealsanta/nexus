# NSH look audit and component pass (plan 05, section 5.3)

Seeded with the offline mock provider (`visual_core.py`: tool_marathon, streaming_rich, question, bash_wait, hello;
scratch `NEXUS10_HOME`, no provider calls). `capture.mjs <home> <port> before|after [tag]` writes
`nsh-{1024,1440}-{dark,light}-{tools,text,question,approval,palette,settings}[-tag]-{before,after}.png`; `tui.sh before`
writes `tui-{120x40,160x48}-{dark,light}-{tools,text,question,approval,palette,settings}-before.{txt,svg}` (real
`nexus-tui --dump-frame`). The first (component) pass left the TUI unchanged, so it had no `after` set; the
2026-11-15 token pass does change it, so it adds light `after` frames for Settings, the palette, completion and text.

## Audit: what looked unfinished (NSH before)

1. Approval card: the parameter label column was 64 px, so `background` wrapped to `backgrou`/`nd`; rows were in
   argument order (`command`, `background`) while the TUI lists them alphabetically (parity bug).
2. Settings rows: the label column was 44% wide, so every description wrapped into a narrow 3-5 line block while the
   row had empty space; rows became uneven in height.
3. Secondary buttons (`go`, Continue, Clear) were a flat fill with no edge, nearly invisible on light surfaces.
4. Segmented control: options touched each other, widths followed the word (`on`/`off` tiny), so controls in
   different rows had different shapes.
5. Header tab titles clip mid-glyph at 1024 px (`Waiting questior`). This is the intentional marquee tab box
   (ledger 2026-11-03) so it was not changed; the fixed-width clip without an ellipsis is still open.
6. Settings pane heading (`Appearance`, 12 px muted) is much smaller than the rows below it; the TUI has no such
   heading. Open.
7. Light theme: selected segment and focused list rows used the same pale beige, low contrast against the gray well.
   **Fixed 2026-11-15 (tokens only, both clients):** `light.interactive.selection` `#f0e3d8` → `#e8d0b9` (warm sand,
   ~1.3:1 against `surface.panel`, where it was ~1.09:1) and `light.interactive.settings_selection` `#ddddd9` → `#d9d9d4`
   (neutral, a step below hover); the 256-colour selection fallback moves 224 → 223. Selected segments and rows now read
   warm, focused Settings rows neutral, so the two states are distinguishable. Evidence: `nsh-1024-light-settings-light-selection-{before,after}.png`,
   `nsh-1024-light-palette-light-selection-{before,after}.png`, all 1024/1440 light surfaces with the `-light-selection-`
   tag, and `tui-{120x40,160x48}-light-{settings,text}-after.*` plus `tui-120x40-light-complete-after.*`.
   `python3 interface/check_theme.py` (and with the NSH overlay) passes.
8. NSH-only content: Settings notes under each row and a `System` theme option; the TUI shows one-line rows and
   `light dark`. Wording/grouping parity decision, open.
9. Palette: disabled commands list their reason inline in the muted row, producing long muted lines the TUI keeps
   in the footer. Open.
10. macOS native app capture: not attempted (needs a signed GPUI/AppKit window and Screen Recording permission;
    cannot be launched non-interactively here). Open.

## Component pass (kit.tsx and consumers)

- `Btn`: secondary and danger buttons get a `border-border` hairline; primary and ghost a transparent border so all
  kinds share one height.
- `Seg`: 2 px gap between options and `min-w-12`, so options have a stable size.
- Approval table: label column fits the longest key (cap 144 px); rows sorted like the TUI.
- Settings rows: label column 56%.
Square corners are kept (ledger 2026-11-03); list rows, cards, status dots and the section header were reviewed and
left as they are.

Compare `*-before.png` with `*-after.png` (approval and settings at both widths and both schemes).
