# 52. First-run onboarding

User request (2026-11-23): when Nexus first opens, show an onboarding: connect a provider (and say it can be changed later),
choose which agent uses which model with hints (scout and quick want small, fast models: luna, haiku, hydrogen class), and
teach the basics of Nexus like a game tutorial.

## Decisions (user, 2026-11-23; Web search step 2026-11-24)

- **When:** once per Nexus home. Core stores `ui.onboarded` (default `false`, `[ui]` in `config.toml`); both clients open the
  tour while it is exactly `false` and write `true` on Finish or Skip, so finishing in one client counts for both. An older Core
  has no key and never triggers it. Replay: `/onboarding`, the palette, Settings > About > Replay onboarding.
- **Form:** a full-window step-by-step view, not a modal, with progress dots, Back / Next / Skip and a goal on the provider
  step ("Connect a provider", checked when one is connected).
- **Agents:** one row per agent with a hint; a picker limited to connected providers' models writes `agents.models.<agent>` (one
  model). After selecting a model with catalog-listed effort levels, an effort picker offers its supported levels and the
  effective agent or model default; `e` reopens it. Overrides are per agent and model. The root row cycles `defaults.agent`
  (`build` / `orchestrate`). Unset means the agent uses its parent's model. There is no "suggest for me" button.
- **Web search (user, 2026-11-24):** desktop onboarding has a Web search step after the agent models. Core starts the
  SearXNG behind `web_search` in the background as a container (Apple `container` when installed, else Docker), using
  the settings packaged in the wheel and copied to `$NEXUS10_HOME/search/`. The step shows running, starting, stopped,
  not set up or unavailable, offers Retry, and lets the user continue without it. The first start downloads the image,
  so the tour does not wait for it. Mobile still has no tour and no search step.
- **Mobile:** excluded (the phone connects to a Core that is already set up). No ledger row needed.

## Shape

- Steps, agent rows, hints and wording live in `interface/manifest.toml` (`[onboarding]`, `{key:<command>}` filled per client), so
  the two clients cannot drift. `check_manifest.py` validates them.
- Commands `app.onboarding` (slash `onboarding`) and `onboarding.back` (Esc rule `onboarding`); term `view.onboarding`.
- TUI: `nexus-tui/src/onboarding.rs` (data, rules, behaviour), `render/onboarding_ui.rs` (drawing), `View::Onboarding`.
  NSH: `src/onboarding.ts`, `src/ui/onboarding.tsx`, mounted in `App.tsx`. The provider step reuses each client's provider
  detail modal; the model picker reuses its picker. The Web search step reads Core's search-service status and starts it
  on entering the step in the TUI; NSH still draws the text only.

## Done

- [x] Core `ui.onboarded` (schema, default, validation, test).
- [x] Manifest steps, command, Esc rule, settings row, checker.
- [x] TUI and NSH views, auto-open, replay, keyboard, mouse.
- [x] Tests: Core `test_config.py`, TUI `onboarding.rs` (unit + `tests/onboarding.rs`), NSH `onboarding.test.tsx`.
- [x] Paired screenshots, 120x36 TUI and 1000x650 NSH (welcome, provider, agents), 80x24 TUI agents scroll.
- [x] Core's packaged SearXNG settings, status and background start; TUI Web search step with state, Retry and
  continue-without-it.

## Not verified

- [ ] NSH's Web search step. The manifest has the step, and NSH can still draw its text, but NSH does not yet query the
  service, start it or offer Retry (the TUI does). What a real first start shows while the image downloads is untested.
- A real sign-in from inside the tour (the provider step opens the same modal as Settings; only the opening is tested).
- A real model pick against live providers, and light theme / narrow NSH windows.
- On a home that already has providers and sessions the tour still opens once (the flag is per home, by decision); the user
  can Skip. Say so if an auto-skip for existing homes is wanted.
- NSH web tests: this machine's runner drops the browser on every second `open` in a file (also `keyboard-flow.test.tsx`
  without my change), so `onboarding.test.tsx` was run one test at a time (`--filter`); all nine pass.
