# Settings: Providers and Agents pages

Settings loses its Providers, Models and Agents sections. In their place are two
entries, **Providers** and **Agents**, each opening a drill-in page inside
Settings (back / Esc returns to the section list). Every other section
(Appearance, Tools, Skills, MCP, Context, Voice, Keybindings, Notifications,
About) stays as it is. Both clients, TUI and NSH, get the same structure and
wording; compare them with screenshots for one design language.

## Decisions (2026-10-30)

- **Shape:** the three sections become two drill-in pages; not a separate centre view.
- **Pools are optional.** An agent with no pool keeps today's rule: its agent
  file's `model`, else the parent's model. The Agents page always shows that
  effective default, so nothing is implicit.
- **Pool sampling is uniform** (two entries = 50/50, three = ⅓ each). No weights.
- **Disconnected providers are skipped, visibly.** Sampling uses only entries
  whose provider is connected; the page marks the others, and the child records
  what was skipped. An all-disconnected pool falls back to the effective default.
- **Fallbacks are subagents only.** Root sessions use the model the user picked.
- **Leftover rows:** "Default agent for new sessions" and "Default effort" go to
  the top of the Agents page; catalog refresh goes to the Providers page.

## Already done (do not redo)

- Core: `[agents.models]` (uniform `secrets.choice`, stored once as the child's
  model) and `[agents.fallbacks]` (frozen in the snapshot as `model_fallbacks`;
  switch only before any stream event, with a `ModelFallback` event and a
  `[Nexus notice: model fallback]` in context). Validation in
  `nexus-core/nexus/config.py` `_valid`; sampling in `runtime.create_session`.
  Documented in docs/config.md and docs/agents.md.
- NSH (`nexus-nsh/src/ui/settings.tsx`): `ProvidersPage` (connect/disconnect,
  API-key entry, device flow polled into toasts) and `AgentModelsPage` (pool and
  fallback lists with add/remove/move-up). They render inside the old
  `providers`/`agents` sections; the `models` section is filtered out by NSH only.
- TUI: nothing new. Settings is still the flat manifest list (`providers.list`,
  `providers.sign_in/out` via the `ProviderLogin` palette, `models.*`,
  `agents.list`, `agents.default_root`).

## Step 1: Core

1. **Provider sign-in metadata.** `GET /api/provider` adds `methods`
   (from `SIGN_INS[id].methods`, minus `browser`), `key_hint` (the
   `ApiKeyAuth.hint`, e.g. "from console.groq.com/keys") and `key_env` (e.g.
   `GROQ_API_KEY`, so the page can say "using $GROQ_API_KEY" when the key comes
   from the environment: add `source: "stored" | "env" | null` too). Clients stop
   hard-coding which providers take a key (NSH `providerAction` today).
2. **Device-flow state for pages.** `provider.login` events and
   `GET /api/provider/{id}/login` already carry `message`; add structured
   `verification_uri` and `user_code` when the flow has them, so a page can show
   the code and link inline instead of a toast. Never include tokens.
3. **Skip disconnected pool entries.** In `runtime.create_session`, filter the
   pool by `connected()` before `secrets.choice`. Record in the snapshot
   `model_pick: {pool, eligible, chosen, skipped}` (skipped = refs whose
   provider was not connected). If `eligible` is empty, use the effective
   default and say so in `model_pick`. Apply the same filter to the frozen
   fallback list (skipped fallbacks recorded beside `model_fallbacks`). Surface
   `model_pick` in the session JSON summary so the parent card/inspector can show
   "picked codex/x from 2 (groq/y skipped: not connected)".
4. **Effective default for the page.** Extend `GET /api/agent` entries (or add
   `GET /api/agent/defaults`) with, per child-capable agent:
   `pool`, `fallbacks`, and `effective: {model, source}` where `source` is
   `pool`, `agent` (frontmatter `model`) or `parent`. Keep the config file the
   single source of truth for edits (`PATCH /api/config agents.models|fallbacks`).
5. **Validation the page relies on:** reject pool/fallback refs whose provider
   is unknown (already partly in `_valid` via `split_ref`; add the provider-id
   check). Unknown agent names stay allowed (a project agent may be absent from
   another project) but are listed in `errors` only if malformed.
6. Tests: `tests/test_children.py` (skip-disconnected, all-disconnected,
   `model_pick` recorded, fallbacks filtered), `tests/test_server.py`
   (provider `methods`/`key_hint`/`source`, device fields, no secrets in events),
   `tests/test_config.py` (unknown provider rejected).

## Step 2: Manifest (`interface/manifest.toml`)

- Remove the `models` section. Replace the rows of `providers` and `agents`
  with a single `kind = "page"` row each (or a section-level `page = true`),
  so clients render an entry that opens a page rather than rows.
- Move `models.default_effort` → `agents.default_effort`; keep
  `agents.default_root` (label "Default agent for new sessions"). Drop
  `agents.list`, `agents.open_file` (or keep "Open agent file" as a per-card
  action, see below), `models.catalog` and `models.refresh` (refresh becomes a
  Providers-page action). Keep `providers.add_custom` as an unavailable row on
  the Providers page.
- Update `nexus-tui/src/manifest.rs` `settings_order_is_fixed` and the NSH
  `sections` filter (remove the `models` special case).

## Step 3: Providers page (both clients)

One row per provider from `/api/provider`, in that order, `mock` hidden:

```
Providers                                             Refresh models
  Codex          Connected · 42 models               [Disconnect]
  Copilot        Not connected                       [Connect]
  Groq           Using $GROQ_API_KEY                 [Disconnect]
  OpenAI         Not connected · API key             [Add key]
  Ollama         Not running locally                 [Connect]
  Add OpenAI-compatible provider   (unavailable: Core does not support it yet)
```

- Status line: connected / not connected, key source (stored vs env), model
  count from `/api/model?provider=`, `verified: false` shown as "unverified".
- Actions follow `methods`: `device` → Connect starts the flow and the row
  expands inline with the URL and code (copy button / OSC 52 in TUI), live from
  `provider.login`; Cancel sends `DELETE`. `api_key` → inline masked field with
  `key_hint`. `local` → Connect checks Ollama. Disconnect asks for confirmation
  when sessions or agent pools use that provider ("scout's pool uses groq/y").
- Failures stay on the row (not only a toast). On connect/disconnect, reload
  providers and the model list, and the Agents page's "not connected" marks.
- TUI: replaces the `ProviderLogin`/`ProviderLogout` palettes as the main path;
  keep `/login` commands working by opening this page.

## Step 4: Agents page (both clients)

Top block:

- **Default agent for new sessions** (`defaults.agent`), picker of
  root-capable agents.
- **Default effort** (`defaults.effort`).

Then one card per child-capable agent (`mode` `subagent` or `all`), from the
project's agent catalog, in catalog order; `problems` listed above the cards:

```
scout                                   built-in · read, bash
Reads the material and returns a context brief.
Default models   (one is picked at random for each new subagent)
  codex/gpt-6        50%                 [Remove]
  groq/llama-x       50%  provider not connected — skipped   [Remove]
  [Add model]
  With no models: uses copilot/claude-sonnet-5 (from the agent file)
Fallbacks        (tried in order if the request fails before any output)
  1. copilot/gpt-6-luna                  [Up] [Down] [Remove]
  [Add fallback]
```

- Percentages are computed over **connected** entries, so the shown odds are
  the real odds; skipped entries show 0% with the reason.
- The empty-pool line shows `effective` from Core (agent file model or "same
  model as the parent session").
- Model picker: the shared model picker filtered to connected providers,
  grouped by provider, excluding refs already in that list. A ref may be in both
  the pool and fallbacks; Core already drops the chosen model from its own
  fallback list.
- Edits `PATCH /api/config` with `expected_revision`; a 409 reloads and retries
  once, then shows the conflict. `config.changed` refreshes the page.
- Per-card "Open agent file" (uses the built-in editor, K10) for global and
  project agents; built-ins show their path read-only.
- Note on the page: changes apply to new subagents; running children keep the
  model and fallbacks they were created with.
- Primary-only agents (`orchestrate`) are not shown as cards; the top picker
  covers them.

## Step 5: TUI specifics (`nexus-tui/src/app.rs`, `render.rs`)

- `settings_items` renders `Providers ›` and `Agents ›` entries; Enter opens a
  page state (`SettingsPage::{List, Providers, Agents}`) inside the settings box;
  Esc/Backspace returns, keeping the cursor on the entry. Settings search
  matches page contents (provider names, agent names, refs) and opens the page
  at the hit.
- Keys on the Agents page: `a` add model, `f` add fallback, `d` remove,
  `K`/`J` move a fallback, Enter on the top rows opens their picker. Show the
  keys in the footer hint line, as other pages do.
- Remove the `models.*`, `agents.list`, `providers.*` arms from
  `settings_wired`, `settings_value`, `settings_change`, `settings_activate`.
- Tests in `nexus-tui/tests/behaviour.rs`: open/close each page, add/remove
  pool entry patches config, disconnected mark, device-code row from a
  `provider.login` event. PTY screenshots at 80×24 and 160×48.

## Step 6: NSH specifics (`nexus-nsh/src/ui/settings.tsx`)

- Replace section rendering for `providers`/`agents` with entry rows that set
  a `settingsPage` signal; the page has a back button in the heading and Esc
  goes back before closing the modal.
- Rework `ProvidersPage` per Step 3 (drop the hard-coded key-provider list and
  the toast polling loop; drive from `methods` and `provider.login` events).
- Rework `AgentModelsPage` per Step 4: card header with source and tools,
  percentages over connected entries, effective-default line, Down as well as
  Up for fallbacks, top block with default agent and effort.
- Tests: update `App.test.tsx`/settings tests for the new sections; add browser
  coverage once the mock stream is fixed (see PLAN.md, Browser tests).

## Step 7: Docs and ledger

- docs/config.md: skip-disconnected rule, `model_pick`, new row placement.
- docs/agents.md: model resolution now mentions skipped pool entries.
- docs/api.md: new `/api/provider` fields, device fields in `provider.login`,
  agent `effective` defaults, `model_pick` in session JSON.
- docs/auth.md: key source (stored vs env) exposure.
- nexus-tui/README.md and nexus-nsh/README.md: Settings layout.
- docs/porting-ledger.md: one row if old Nexus settings differed.
- PLAN.md: remove the Providers bullet "add API-key entry in TUI Settings"
  once Step 5 lands; delete this file when all steps land.

## Order

1 → 2 → (3, 4 in NSH, since its pages exist) → 5 (TUI) → screenshots of both
→ 7. Steps 1.3 and 1.4 can land before any UI change; the current NSH page
benefits immediately.

## Open (decide while building)

- Whether to show a small per-agent "last picked" history (from child
  sessions' `model_pick`) on the card. Default: no.
- Whether disconnecting a provider used by a pool should offer to remove those
  entries. Default: only warn; entries stay and are skipped.
