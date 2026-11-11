# 8. Provider usage panel (complete)

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

The user's review of the Provider usage modal (screenshot, 11-06) asks for a table instead of the current
label + bar + prose rows: a ten-block progress gauge, then the percentage left, the reserve (the old Nexus
pace calculation) and the expiry time. The words marked in red go away. Both clients land this in the same
change; no desktop client-specific difference is expected. **Landed and visually compared in both clients.** Carried over whole from the previous plan's section 16 (user asked to keep it, 2026-11-06).

### 8.1 The table and its cells (both clients)

One header row, then a provider group row and one column-aligned row per window. The gauge shows *used*
and the next column states what is left, so the two add to 100%:

```text
Window                                  Progress    Left  Reserve  Expires
ChatGPT (Codex)  Plus
  5-hour                                ██████████    0%      —    1h 8m
  Weekly                                ██▃░░░░░░░   76%     -1%   5d 9h
Claude (Agent SDK)  Pro
  5-hour session                        ███▅░░░░░░   64%      —     now
  Weekly (all models)                   ██████▆░░░   32%    +11%   1d 12h
xAI  SuperGrok Lite
  Weekly                                ▁░░░░░░░░░   99%    +30%   4d 20h
GitHub Copilot  Business
  Premium requests (monthly)            ▇░░░░░░░░░   91%     +8%   24d 18h
OpenCode Go
  5-hour                                ░░░░░░░░░░  100%      —    1h 6m
  Weekly                                ███▂░░░░░░   67%    +28%   2d 18h
  Monthly                               ████▆░░░░░   53%    -10%   19d 0h
Updated 2m ago
r refresh · esc close
```

- [x] **Header row:** `Window`, `Progress`, `Left`, `Reserve`, `Expires`, muted, once above the first
  provider. Provider group rows keep the bold name and the muted plan (`ChatGPT (Codex)  Plus`); its window
  labels are indented two cells. Do not repeat the provider on every window.
- [x] **Progress (the wide column, exactly 10 cells):** `used_percent` decides the fill in eighths:
  `e = round(used × 80 / 100)` (nearest, ties to even), then ten cells filled left to right, each cell
  holding `min(8, max(0, e − 8·i))` eighths. Full cells are `█` (U+2588); the boundary cell is
  `▁▂▃▄▅▆▇` (U+2581–U+2587), so it fills **from the bottom up** as requested; empty cells are `░`
  (U+2591). 24% → `██▃░░░░░░░`, 36% → `███▅░░░░░░`, 100% → ten `█`, 0%/unknown → ten `░`. The bar is
  horizontal because the table is one line per window; a literal ten-row-tall gauge would make every
  window ten lines high. Glyphs come from the block-elements range the user linked
  (`https://unicode-explorer.com/b/2580`).
- [x] **Left:** `100 − used_percent`, following the old Nexus `percent()` rule: a whole percent, except a
  non-whole value under 10 or over 90 keeps one decimal (`0.2%`, `99.8%`); unknown is `—`. Right-aligned.
  This replaces the old used-percent column (42% used now reads 58%).
- [x] **Reserve:** copy the `nexus01` calculation from `nexus/ui_support/usage.py`
  (`RESERVE_MIN_PERIOD`, `elapsed_percent`, `reserve`, `reserve_phrase`): only when `period_seconds` is
  at least 7 days and `used_percent` and a future `resets_at` are known;
  `elapsed = 100 × (1 − (resets_at − now) / period_seconds)`, `reserve = elapsed − used`. Round to the
  nearest integer with ties to even, as Python's `round` does (8.5 → 8). Show `+N%` for spare usage,
  `-N%` for over pace, `0%` for on pace and `—` when not estimable (five-hour windows, missing period, a
  reset already due). Right-aligned; spare takes the success colour, over pace the warning colour,
  `0%`/`—` stay muted. Do not show the old `┃` pace marker.
- [x] **Expires:** the time until reset only, never an absolute stamp: `now` under a minute or when due, otherwise the
  two largest units (`3h 10m`, `2d 4h`, `6d 5h`); `—` when `resets_at` is missing.
  Left-aligned. This is the old `resets in …` text without the words.
- [x] **Colours and tone:** filled gauge cells keep the existing used-percent thresholds (success below
  70, warning below 90, error at 90 and above); empty cells use the dim border/muted role. The label and
  the Left number stay in primary text; the provider plan, header, `—` and notes are muted.
- [x] **Words removed (the red marks):** drop the Codex `Limit reached: new requests wait for the next
  reset` note, the xAI per-product notes such as `GrokChat: 1% of credits`, and every per-window `detail`
  suffix (`225 of 300 left`, `20,280 over quota`, `· 20,28…`). `detail` stays in the API for other
  consumers but is never printed by this modal; when a window has no `resets_at`, Expires is `—`
  rather than the old raw-text fallback. Keep the other notes (credits balance/unlimited, no plan
  limits, `Could not read the limits: …`, `No limit windows reported.`) as muted full-width lines under
  their provider, plus `Updated … ago`, the empty and loading states and the `r refresh · esc close`
  footer.
- [x] **Responsive rules:** the gauge and the three numbers keep their widths; the Window column flexes
  and truncates with `…`; Expires truncates with `…`. Never wrap a window row. The TUI keeps its
  80–88-cell large modal and 30-row cap. NSH uses its large modal as `tall` (90% of the window) so the
  table fits; on phone widths (a standing phone-layout exception) each window becomes two lines: label,
  then gauge, Left, Reserve and Expires.

### 8.2 Data and formatting

- [x] `period_seconds` and `kind` are already in the API (`GET /api/usage`); parse them in both clients.
  The reserve is derived client-side from `used_percent`, `resets_at`, `period_seconds` and the client's
  `now`, beside the countdown; do not add a server endpoint or store a stale reserve in the snapshot.
- [x] Unknown `used_percent` → ten empty cells, `—` Left, `—` Reserve, and the countdown if known.
  Missing `period_seconds` (older snapshots) → Reserve `—`, everything else unchanged. A reset at or
  before `now` → Expires `now`, Reserve `—`.
- [x] Filter the removed notes in both clients as well as at the source, so an older saved snapshot
  cannot bring them back: Codex notes starting `Limit reached:`; xAI notes ending ` of credits` that
  are not `Prepaid credits:`. The existing xAI prepaid and Copilot chat/completions/AI-credit filters
  stay.
- [x] Keep the snapshot semantics: the modal is a snapshot; countdowns and reserve do not tick while it
  stays open (the existing background-refresh follow-up remains separate).

### 8.3 Core

- [x] `nexus-core/nexus/provider_usage.py`: remove the `limit_reached` note in `parse_codex` and the
  `productUsage` note loop in `parse_xai`; keep the credits and prepaid notes. Keep
  `parse_copilot`/`parse_claude` `detail` values (the API keeps them; the modal stops printing them).
- [x] Update `nexus-core/tests/test_provider_usage.py` with a `limit_reached: true` payload and a
  `productUsage` list, asserting neither becomes a note; keep the existing prepaid and detail assertions.
- [x] `docs/api.md` (Usage K11): say the modal is a table of gauge + percent left + reserve + expiry;
  `detail` is retained for other consumers but not shown; note the reserve rule and its 7-day threshold.

### 8.4 TUI (`nexus-tui`)

- [x] `src/usage.rs`: extend `Row` with a `Header` row and make `Window` carry the label, the used value,
  the formatted Left, Reserve and Expires strings (computed from `now`). Parse `period_seconds` and
  `kind`. Drop the `detail` join from the reset text. Add pure helpers for `gauge_cells(used) -> [u8;
  10]`, `percent_left`, `reserve`, `reserve_text` and `duration` with unit tests at 0, 1, 9, 10, 24, 36,
  47, 68, 90 and 100 percent; five-hour vs weekly/monthly; a missing/old period; a reset in the past;
  unknown usage; and the half-to-even tie (8.5 → 8).
- [x] `src/render.rs::draw_usage`: draw the header and the five fixed columns, map `gauge_cells` through
  the theme glyphs, colour only the filled cells, right-align Left/Reserve, left-align Expires, and
  truncate the label and Expires. Keep `MODAL_LARGE`, the height clamp and the existing `r`/`esc` hits.
- [x] ASCII mode: ten `#`/`-` cells; a partially filled cell counts as `#` (rounded up). Colour rules are
  unchanged.
- [x] `interface/theme.toml`: add `[glyphs.usage]` with `full` (`█`/`#`), `empty` (`░`/`-`) and
  `eighth_1`…`eighth_7` (`▁`…`▇`/`#`); add `usage` to the glyph checks in `interface/check_theme.py`,
  the `Glyphs` struct/accessor in `src/theme.rs`, and the generator in `nexus-nsh/scripts/gen.py`.
- [x] `tests/behaviour.rs` (and the usage unit tests): assert the header, the gauge strings, `76%`/`0%`
  Left, reserve cells and the hidden `Limit reached` / `of credits` / `of 300 left` text.
- [x] `nexus-tui/README.md`: update the K11 bullet to the table.

### 8.5 NSH (`nexus-nsh`)

- [x] `src/store.ts`: add `period_seconds?: number | null` and `kind?: string` to the usage window type.
- [x] New pure `src/usage-format.ts` (or exports beside `until`): `leftPercent`, `reservePercent`,
  `reserveText`, `gaugeCells` and `until` with the same rounding and threshold rules as the TUI.
- [x] `src/ui/usage.tsx`: replace the flex bars with a monospace table: header row, provider group rows,
  window rows, 10-cell gauge drawn from the generated `usage` glyphs, fixed-width Left/Reserve/Expires
  columns and a flexing Window column. Mark the gauge `accessible={false}` and give each row an
  accessibility label with all four values. Use `Modal … tall`; keep the existing refresh/footer and the
  loading/empty/error states. Hide the removed notes in `notes()` for older snapshots.
- [x] `usage.test.tsx`: update the expected percentages to the Left values (42% used → 58%), assert the
  header, a gauge string such as `██▃`, `—`/reserve cells, and the absence of `Limit reached`, `of
  credits` and the Copilot detail. Unit-test the pure formatting helpers with a fixed clock (including
  8.5 → 8 and the 7-day threshold).
- [x] `nexus-nsh/README.md`: update the K11 bullet to the table.

### 8.6 Shared scenario and screenshots

- [x] Extend `interface/scenarios/provider-usage.json` with the values above: Codex 100%/24%, Claude
  36%/68%, xAI 1%, Copilot 9%, OpenCode 0%/33%/47%, plus the notes that must disappear (`Limit
  reached: …`, `GrokChat: 1% of credits`), a provider with no plan limits and one with an error.
  Keep time-dependent assertions in the pure helper tests; the scenario test asserts labels, Left
  values, gauge glyphs and the hidden words.
- [x] Add a usage fixture to `nexus-nsh/e2e/scripted_core.py` that returns the scenario with
  `resets_at`/`period_seconds` computed relative to the capture time (monkeypatch
  `nexus.provider_usage.providers_usage` or seed the usage cache), so both clients see identical rows
  without provider calls. Document the mode in the script's docstring.
- [x] Capture the same state in both clients: TUI `--dump-frame` at 170x50 and 120x36, NSH web at a
  comparable window (plus one phone-width capture). Compare the header, column order, gauge glyphs,
  numbers, colours and the absence of the marked words; save under `reports/provider-usage-table/`.

### 8.7 Checks when it lands

- [x] `cd nexus-core && uv run pytest tests/test_provider_usage.py -q && uv run ruff check nexus/provider_usage.py tests/test_provider_usage.py`
- [x] `cd nexus-tui && cargo test usage && cargo test --test behaviour usage && cargo fmt --check && cargo clippy --all-targets -- -D warnings`
- [x] `cd nexus-nsh && npm run gen && nsh test usage.test.tsx --target web` (plus the pure helper test and `nsh check`; record the known `app.clock` / `Button.onFocus` errors if they remain).
- [x] `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml && python3 interface/check_manifest.py`
- [x] Update `docs/porting-ledger.md` on landing: "Provider usage showed a continuous bar, used percent and prose notes | Both clients show a five-column table: ten-block gauge filled bottom-up, percent left, reserve (old 7-day pace formula), expiry; the Codex limit-reached note, xAI per-product credit notes and per-window detail text are gone (2026-11-06) | User request". Record any screenshot-only difference; none is expected on desktop.
- [x] Tick this section, move its one-line record to Landed and remove the status-table row's "planned" wording.
