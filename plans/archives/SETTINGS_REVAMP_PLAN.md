# Settings revamp (native Ratatui client)

Status: superseded by plans/RATATUI_DESIGN_MOCKUPS_PLAN.md (one-page Settings). Earlier status: steps 2, 5, 6, 7 and 8 landed (navigation core, run-ordered tiers, default
chain, Session titles, agent run mode); the typed-row visual design (§3.4), shortcut
rows and native PTY/screenshot checks remain pending.
Scope: **the native Ratatui terminal client only**
(`nexus chat`). The web app (to be deprecated) and the GPUI desktop client are out
of scope; nothing here is ported to them.

Goal: Settings should look polished, show clearly what each setting is and where it
is saved, never close unexpectedly, and be usable with the arrow keys alone. Models
and agents must show the model that will actually run, and let the user reorder
every model list in place.

### Progress

- Session titles saves now return to the existing titles page without discarding
  its parent navigation history, for both enabled toggles and model selection.
  Escape can still return to the page that opened Session titles.
- Regression coverage includes both save paths and entering the model picker.
  Tier-page and workflow tests: 63 passed.
- Provider closing, editor journeys, focus/rendering, ordered model routing and
  the rest of this plan remain pending; native PTY behavior is not verified.

## 1. What the user asked for

1. Up/Down move through the **area list** (the left headers) as well as through a
   page's rows. Left/Right move focus between the area list and the current page.
2. Remove **Workspace**, **Config**, **Soul** and **Hooks** from Settings for now
   (they come back later).
3. Clicking a **provider** must never close Settings. It sometimes does today.
4. **Session titles** must work properly.
5. **Models** must work properly. The row for `low` names the right model, but
   opening it shows a different one. Opening a tier must show the model in use
   and its fallbacks, each with up/down controls to reorder. The **default model**
   gets the same ordered list (the top is used, the rest are fallbacks).
   `medium` and `high` work the same way.
6. **Agents**: a subagent runs on either a **specific model** (with ordered
   fallbacks) or a **tier**, which picks a model automatically from the
   providers that are connected.
7. Selecting an area (for example Config) must never close Settings.
8. Settings must look much better. Today it is plain menu rows, and it is hard to
   tell what a row is, what its value is and where it is saved.

## 2. How it works today (findings)

Code paths: `nexus/ui/ratatui/workflows.py` (pages and `operate`),
`nexus/ui/ratatui/tier_pages.py` (Models, Session titles, agent tiers),
`nexus/ui/ratatui/prototype.py` (`_settings_nav`, `nav_select`, action dispatch),
`nexus/ui_support/settings_help.py` (`SETTINGS_SECTIONS`, help text),
`nexus/ui_support/tier_settings.py`, `nexus/host_support/model_settings.py`,
`rust/tui/src/main.rs` (keys and mouse), `rust/tui/src/render.rs` and
`rust/tui/src/render/dialogs.rs` (drawing, `nav_rect`, `nav_step`).

### 2.1 Navigation
- The area list is a Python projection (`_settings_nav`). It is shown only while
  `shell.settings_nav` is not `None`.
- `Workflows.operate` sets `settings_nav = None` for **any** operation whose kind
  does not start with an entry in `NAV_KEEP`. That one rule decides whether the
  page still counts as Settings, so a new or overlooked operation kind quietly
  drops the area list and turns Settings into an ordinary panel. This is the most
  likely cause of "Settings closes sometimes".
- Left/Right call `nav_step` and **switch area at once**. Up/Down only move within
  the page's rows, and the area list never takes focus.
- The area list is 24 columns wide and has no focus state, only "selected".
- `menu()` defaults to `layout="drawer"`. Settings looks like a modal only because
  the renderer forces it when `nav` is present. Mouse hit-testing uses the same
  `panel_area`, but on a snapshot that can be stale while a page is loading.
- `Workspace` calls `shell.show("Workspace", doctor())`, which replaces the page with
  a key/value dump. `Keyboard` runs `/hotkeys`, which is not a Settings page.
- With `nav` present, the renderer shows **only the first two** `panel_lines`
  (truncated) above the rows. Tier notes, agent conflict notes, the title model's
  error message and provider status lines are drawn **nowhere**. This breaks the
  rule that nothing the agent can see is hidden from the user, and it explains part
  of "hard to see what is going where".

### 2.2 Providers closing Settings (unconfirmed)
Not reproduced yet. Candidates, in order:
1. An operation reached from the provider page whose kind is missing from
   `NAV_KEEP`, or a `shell.show(...)` call (as in `default_agent_save` and
   `workspace`) that replaces the page and clears `items`.
2. A mouse click handled against a stale snapshot. During `panel_loading`,
   `panel_area` or the row index can differ, so the click lands outside the area
   and triggers `dismiss`.
3. An operation that is not in `allowed_operations` (items rebuilt between the
   snapshot and the click) is dropped silently. That fits "nothing happens" better
   than "closes", but it should be ruled out too.

Step 1 of the work is to reproduce this with the Rust trace (`rust/tui/src/trace.rs`
and `nexus/ui/ratatui/trace.py`) and a PTY journey before changing code.

### 2.3 Config closing Settings (unconfirmed)
Config, Soul and Hooks are removed (§3.1), but they used the same
`settings_read → edit()` path that Tools, Skills and MCP files still use. The cause
must be found and fixed for those pages. Reproduce it in a PTY journey: area →
item → editor → Escape.

### 2.4 Models: the label and the page disagree (confirmed by reading code)
- The Models row label uses `resolved`, which is `router.resolve(tier)`. For an
  unpinned built-in tier, the router walks `DEFAULT_TIER_MODELS[tier]`
  (`nexus/model/tiers.py`; the ordered routes in docs/models.md "Default tier
  routing") and uses the first model whose provider is connected.
- The tier page lists `refs` from `tier_rows()`, which reads `tiers.builtin`
  (`BUILTIN_TIERS`, the curated **classification** map). That is a different list,
  in dict order, and it is not the route order.
- So `low · runs on github-copilot/gpt-6-luna` can open a page whose
  `1. … · used first` is some other model. A price-based tier shows an empty list
  even though it runs on something.
- The page also never says which entries can run (provider connected) and which
  are skipped.

### 2.5 Default model
There is no ordered default-model list in Settings. Setup (`SetupSave`) writes one
model. `models.default` and `models.fallback` exist in config (docs/config.md) but
are edited only through the raw config file.

### 2.6 Agents
- The agent page mixes `Model`, `× Clear model`, a `Tiers` row, `Fallback N` and
  `× Remove fallback N` rows. Pinning a model **and** setting tiers is allowed and
  only produces warning notes, which are hidden (§2.1).
- The fallback order can be changed only by clearing and re-adding entries.
- The tiers page uses `[x]` toggles plus "Make X the default" rows instead of an
  ordered list.

### 2.7 Session titles
No specific bug is confirmed from the code. Known issues: the resolved title model
inherits the Models mismatch (§2.4); the explanation and the host's `message` line
are cut off by the two-line limit (§2.1); `title_set` and `title_model_set` clear the
whole stack. Step 1 includes an end-to-end check that a new session actually gets a
generated title from the chosen model, and that turning it off falls back to the
first line.

## 3. Target design

### 3.1 Area list (left pane)

```
 GENERAL            │  Models
   Appearance       │  Which model runs by default and for each tier.
   Layout           │  Saved to ~/.nexus/config.toml
   Keyboard         │  ─────────────────────────────────────────────
                    │  DEFAULT MODEL
 MODELS             │  ● 1  openai-codex/gpt-6.1-sol        in use   ↑ ↓ ×
   Providers        │    2  claude-agent/claude-sonnet-5.5  ready    ↑ ↓ ×
 ▸ Models           │    3  opencode-go/deepseek-v4.1-flash not connected ↑ ↓ ×
   Session titles   │    + Add fallback…
                    │
 AGENTS & TOOLS     │  TIERS
   Agents           │    low     github-copilot/gpt-6-luna   3 models  ›
   Tools            │    medium  openai-codex/gpt-6.1-sol    2 models  ›
   MCP servers      │    high    openai-codex/gpt-6.1-sol    2 models  ›
   Skills           │    Highest tier for subagents          high      ›
                    │
 VOICE              │
   Voice            │
   Speech           │
 ───────────────────┴──────────────────────────────────────────────────────
 ↑↓ move · → open page · ← areas · Enter select · Esc back/close
```

- Sections: **GENERAL** (Appearance, Layout, Keyboard), **MODELS** (Providers,
  Models, Session titles), **AGENTS & TOOLS** (Agents, Tools, MCP servers,
  Skills), **VOICE** (Voice, Speech).
- Removed: Workspace, Config, Soul, Hooks. They are dropped from
  `SETTINGS_SECTIONS`, from the root `settings_menu` rows, from `NAV_AREAS` and from
  `SETTINGS_HELP`. The host categories stay as they are; only the native client
  stops listing them. The `tier_lines` text "edit it in Settings → Config" changes
  to name the config file path.
- The area list widens to fit its longest label plus padding (about 22–26 columns)
  and has a vertical divider.

### 3.2 Focus and keys

There are two focus zones, **areas** and **page**. The focused zone draws its
selected row in the accent colour; the other zone keeps a dim marker.

| Key | Areas focused | Page focused |
| --- | --- | --- |
| Up / Down | previous / next area (headings skipped); the page **previews** live | previous / next row (group headings skipped) |
| Right / Enter | focus the page (first row) | Right: enter a row's sub-page; Enter: activate the row |
| Left | (nothing) | back to the areas zone. On a sub-page, Left pops to the parent page first; at the area's root page it focuses the areas |
| Esc | close Settings | pop a sub-page; at the area's root page, focus the areas |
| Space | — | toggle switches (on/off rows) |
| Shift+Up / Shift+Down (also `K` / `J`) | — | move the selected item up/down in an **ordered list** (models, fallbacks, tiers) |
| Delete / Backspace | — | remove an item from an ordered list (with undo notice) |
| Type | filter areas | filter rows (as today) |

- Focus state lives in Rust (local, like `panel_detail`). Changing the area sends
  `nav_select`. Moving Up/Down in the areas zone sends it after a short debounce
  (about 120 ms) so holding the key does not flood the host.
- In an editor form, Left/Right move the cursor as today. Esc leaves the form, and
  if it has unsaved changes the existing "Discard unsaved changes?" page appears.
- The footer hint row always shows the keys for the focused zone.

### 3.3 Settings never closes by accident

- Replace the `NAV_KEEP` prefix list with an explicit flag. Every page that
  `Workflows` builds while Settings is open carries `settings=True`, and only an
  explicit `close_panel` or `dismiss` at the root, or a chat command that leaves
  Settings, clears it. Leaving Settings becomes opt-in instead of the default.
- `shell.show(...)` is never used inside Settings. `default_agent_save` and similar
  confirmations re-render the page with a notice.
- Every Settings page uses one layout (`settings`), so it never depends on the
  drawer/modal default and mouse hit-testing uses the same rectangle as drawing.
- Mouse clicks are ignored while `panel_loading` is set, or while the snapshot
  revision is older than the last action, so a click is never resolved against a
  stale layout.
- An operation that is dropped because it is not in `allowed_operations` sets a
  notice ("That option changed; choose again") instead of failing silently.
- Errors from `operate` keep the current page and show the error inline on the
  page (red notice row), never by clearing the panel.

### 3.4 Page anatomy (visual)

Every page has the same structure, drawn by Rust from a structured projection
instead of plain `label · value` strings:

1. **Header**: page title (bold), a one-line description, and the **save
   location** (`Saved to ~/.nexus/config.toml`, `~/.nexus/agents/task.md`,
   `this terminal`). The scope (global/project) is shown as a chip when the area
   has both.
2. **Groups**: an upper-case dim heading (`DEFAULT MODEL`, `TIERS`, `SIGN-IN`) and
   a thin rule.
3. **Rows**, each typed:
   - `toggle`: label on the left, a right-aligned `● on` / `○ off` switch.
   - `choice`: label on the left, the current value right-aligned in the accent
     colour, then `›` (opens a picker).
   - `ordered`: position number, value, a status chip (`in use`, `ready`,
     `not connected`, `unknown model`), and right-aligned `↑ ↓ ×` controls that
     are clickable and reachable by keyboard (§3.2).
   - `action`: `+ Add fallback…`, `Reset to default…` in a muted accent;
     destructive actions are red and always confirm.
   - `status`: read-only labelled line (`Connection: connected · ChatGPT
     account`).
4. **Notes**: every note, warning and host message, fully wrapped (no two-line
   cap). Warnings (yellow) and errors (red) appear above the rows they refer to.
   Long pages scroll, and the scroll position is announced (`↓ 4 more`).
5. **Footer**: key hints for the focused zone, plus a transient notice
   (`Saved`, `Saved · restart the daemon to apply`).

Projection change: each item gains optional `type`, `value`, `status`, `tone`
and `controls` fields beside `label` and `operation`. The Python side
(`ui_support/settings_rows.py`, new) builds them; Rust renders them. Old plain
items still render as today, so pages can move over one at a time.

### 3.5 Providers

- The list: one row per provider with a status chip (`connected` green,
  `not connected` dim, `sign-in pending` yellow) and the account or method in use.
- The provider page: a header with the connection status; a **SIGN-IN** group (API
  key, browser sign-in, device code, each with a one-line explanation); a
  **CONNECTION** group (Sign out…, Resume sign-in). Sign-in screens (device code,
  paste code, browser) open as sub-pages **inside Settings**, with the area list
  still visible, and return to the provider page when done or cancelled.
- After a successful sign-in, show "Models from this provider are now available"
  with a shortcut row to Models.

### 3.6 Models

The host returns the lists **in the order they actually run** (§4.1).

- **Default model** group: an ordered list of `models.default` followed by
  `models.fallback`. Row 1 is the default and the rest are fallbacks, tried in
  order. Each row shows a status chip, and the first runnable row is marked
  `in use`. Rows offer `↑ ↓ ×`, followed by `+ Add fallback…` and
  `Reset to default…`. If the list is empty, the page explains that a new session
  uses the first connected provider's newest model and names that model.
- **Tiers** group: one row per tier showing the model **in use** (the same value the
  router resolves), the list length and `›`. The tier page shows:
  - Source: `your list`, `built-in route` or `by price`.
  - The ordered list, with the first runnable entry marked `in use`, other
    runnable entries `ready`, and the rest `not connected`. Every entry has
    `↑ ↓ ×`.
  - Built-in routes are shown as they are: editing a built-in list (move, remove,
    add) first copies it into `[models.tiers]` as "your list", and a note says so.
  - A price-based tier lists the model it resolves to and explains that adding a
    model pins the tier.
  - `+ Add model…` (picker grouped by provider, with connected providers first)
    and `Reset to default…`.
- **Highest tier for subagents** stays a `choice` row.
- Reorders save immediately through the host and keep the selection on the moved
  row, so Shift+Up pressed several times keeps moving the same model.

### 3.7 Session titles

- `Generate titles automatically` (`toggle`).
- `Title model` (`choice`): the value shows `low → github-copilot/gpt-6-luna`, the
  same resolution as Models. The picker lists tiers first (each with what it runs
  on) and then models grouped by provider.
- A status line shows whether titles can be generated right now; the host's
  `message` is shown in full when they cannot.
- Toggle and choice updates re-render in place without clearing the stack.
- End-to-end check (§6): a new session gets its title from the chosen model, and
  with the setting off it uses the first line.

### 3.8 Agents

The **Agents** list has groups **ROOT AGENTS** (build, orchestrator, custom roots)
and **SUBAGENTS** (advisor, task, quick, custom). Each row shows what the agent runs
on (`tier low`, `openai-codex/gpt-6.1-sol`, `session model`) and a `built-in` or
`edited` chip. `New sessions start with…` stays at the top. `New agent…` and
`Reset all…` are at the bottom.

**Subagent page**:

```
 Agent · task                         Saved to ~/.nexus/agents/task.md (override)
 Runs routine multi-step work for the root agent.

 RUNS ON
   Mode                      ( ) Specific model   (●) Tier            ‹ ›
 TIERS  (the first is used unless the caller asks for another)
   ● 1  low      → github-copilot/gpt-6-luna          in use   ↑ ↓ ×
     2  medium   → openai-codex/gpt-6.1-sol           ready    ↑ ↓ ×
     + Add tier…
   ⚠ high is above the global limit (medium): it runs as medium.   (notes, if any)

 PROMPT
   Edit prompt file…                                             ›
   Reset to built-in…
```

- **Mode** is a two-value `choice` row: Left/Right or Space switches it while the
  row is selected.
  - **Tier**: an ordered tier list (`↑ ↓ ×`, `+ Add tier…`); each tier shows the
    model it resolves to now. Saving writes `tiers: [...]` and **removes** `model`
    and `fallback`, so the agent always picks from its tiers using whatever
    providers are connected.
  - **Specific model**: an ordered list where row 1 is the model and the rest are
    fallbacks (`↑ ↓ ×`, `+ Add fallback…`, at most 1 + `MAX_FALLBACKS`). Saving
    writes `model` + `fallback` and **removes** `tiers`.
  - Switching mode asks for confirmation if it would discard a non-empty list, and
    keeps the discarded values in memory so switching back within the same visit
    restores them.
- An existing file that has both `model` and `tiers` opens in **Specific model**
  mode with a warning ("This agent also lists tiers; choosing a mode removes the
  other").
- **Root agents** have no Tier mode. Their options are **Session model** (inherit)
  and **Specific model** (with ordered fallbacks).
- The raw frontmatter fields (`provider`, `reasoning_effort`) are shown as
  read-only status rows when present, so nothing in the file is hidden. They are
  edited in the prompt file.

### 3.9 Appearance, Layout, Keyboard, Voice, Speech, Tools, MCP, Skills

- Appearance: the theme as a `choice` row (Dark/Light) with a preview swatch row.
  Layout: `toggle` rows with their shortcut shown as a dim chip.
- Keyboard: an in-page, read-only table of shortcuts grouped by area (from
  `ui_support/shortcuts.py`) instead of opening `/hotkeys` outside Settings.
- Voice and Speech: already grouped; they move to typed rows (toggles, choices, a
  model status line with download size, and confirm-before-download kept).
- Tools, Skills, MCP: file rows show name, scope chip, `built-in`/`edited` and the
  path. MCP rows show the loading mode, tool count and status as chips. Opening a
  file uses the editor inside Settings (§2.3 fix). `Switch to project/global`
  becomes the scope chip in the header (click or `Ctrl+P`).

## 4. Host changes

All UI data and actions go through `nexus/host/protocol.py` and
`nexus/host/facade.py` (AGENTS.md rule 4).

### 4.1 Tier rows in run order (fixes §2.4)
`model_settings.tier_rows` returns, per tier:
- `refs`: the ordered list the router actually walks. Use the user's
  `[models.tiers]` list when pinned, else `DEFAULT_TIER_MODELS[tier]`, else
  (price-based) the resolved model alone.
- `entries`: `[{ref, runnable, reason}]` (`reason`: `in use`, `ready`,
  `provider not connected`, `unknown model`).
- `source`: `your list` | `built-in route` | `by price`.
- `resolved`, as today.

Add a router helper (`ModelRouter.tier_candidates(tier)`) that returns the ordered
candidates with a runnable flag, so the page and `_resolve_tier` share one code path
and cannot disagree again. Add a test asserting that `entries[first runnable].ref ==
resolved` for pinned, built-in and price tiers.

### 4.2 Default model chain (new)
- `ModelDefaults` → `{refs, entries, resolved, source}` for `models.default` plus
  `models.fallback`.
- `ModelDefaultsSet(refs)` writes `models.default = refs[0]` and
  `models.fallback = refs[1:]` to global `config.toml` through
  `provider_auth.write_global_keys` (hash-checked, bounded to 16 refs, validated
  like `_validate_refs`), then rebuilds routes while no turn runs, as `tier_set`
  does. Also add `ModelDefaultsReset`.
- Check how `[model]` vs `[models]` `default`/`fallback` interact (docs/config.md
  lists both). Pick the one the router reads and document it.

### 4.3 Agent run mode
No new host command is needed: it stays a frontmatter write through
`settings_write` with the hash check. `ui_support/agent_frontmatter.py` gains
`set_run_mode(body, mode, items)`, which removes the other fields. Tests confirm the
host loader accepts both shapes and that a tier-only role resolves through
`TierDecision`.

## 5. Implementation steps

1. **Reproduce and pin down the bugs.** PTY journeys (`tests/test_ratatui_pty.py`,
   `tests/test_ratatui_journeys.py`) for: provider click → page stays in Settings;
   area → file → editor → Escape stays in Settings; Models `low` row value equals
   the tier page's `in use`. Record the provider-close cause in this plan.
2. **Navigation core.** Explicit Settings flag in place of `NAV_KEEP`; the
   `settings` layout; areas/page focus in Rust; the key table in §3.2; stale-click
   guard; inline errors. Update `SETTINGS_SECTIONS` (new groups, removals).
3. **Typed rows projection.** `ui_support/settings_rows.py`, item fields
   `type/value/status/tone/controls`, and Rust rendering of header, groups, rows,
   notes (full, wrapped) and footer. Port Appearance and Layout first as the
   reference pages.
4. **Ordered list control.** Shared Python helper (move/remove/add plus selection
   follow) and Rust `↑ ↓ ×` rendering with mouse hit-testing.
5. **Host: tier run order + defaults chain** (§4.1, §4.2) with tests.
6. **Models page** (§3.6) on the ordered control.
7. **Session titles** (§3.7) and the end-to-end title check.
8. **Agents** (§3.8): run mode, ordered model/fallback and tier lists, root vs
   subagent, all frontmatter shown.
9. **Providers** (§3.5): status chips, sign-in sub-pages kept inside Settings.
10. **Remaining pages** (§3.9).
11. **Docs and cleanup.** `docs/ratatui-parity.md` (Settings section rewritten),
    `docs/cli.md` (Settings keys), `docs/models.md` (tier rows show run order;
    default chain), `docs/agents.md` (run mode), `docs/decisions.md` (why Config,
    Soul, Hooks and Workspace are hidden for now; why a mode switch removes the
    other fields), `docs/module-map.md` for new modules.

Each step lands with its tests and can be shipped by itself. Steps 2–4 come before
the page work because every page depends on them.

## 6. Verification

- Unit tests: `test_model_tiers_settings.py` (run order, entries, defaults chain),
  `test_ratatui_tier_pages.py` (reorder keeps selection, built-in copy-on-edit),
  `test_ratatui_workflows.py` (no operation clears the Settings flag except
  close), agent frontmatter mode switches, the `settings_help` section list.
- Rust tests in `render.rs` and `dialogs.rs`: focus zones, `nav_step` with Up/Down,
  typed row layout at 80/120/200 columns, notes wrap without a cap, and hit-testing
  that matches drawing for `↑ ↓ ×`.
- PTY journeys: keyboard-only tour of every area; provider sign-in flow
  cancelled and resumed without leaving Settings; reorder a tier with Shift+Up
  and confirm `config.toml`; switch a subagent between Tier and Specific model
  and confirm the file.
- Screenshots of each page in dark and light themes under
  `artifacts/ratatui-parity/settings-*.png`, inspected before calling it done.
- `ruff check nexus tests` and `.venv/bin/python -m pytest -q`.
- Not verifiable offline: real OAuth/device sign-in and real title generation
  against a live provider. Say so in the parity doc.

## 7. Open questions

1. Default model scope: the global `config.toml` only, or also a project override
   when Settings is in project scope? Proposed: global only for now, like tiers.
2. Should editing a built-in tier route copy the whole list into your config
   (proposed), or only record the change?
3. Should Keyboard become editable later (rebinding), or stay a reference page?
4. Should removed areas (Config, Soul, Hooks, Workspace) stay reachable through a
   chat command (`/config`, …) meanwhile, or be fully hidden?
