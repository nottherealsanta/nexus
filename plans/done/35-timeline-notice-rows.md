# 35. Timeline: icons for single items, "Subagent done" rows (complete)

User request (2026-11-11), from a screenshot of a session timeline (light theme) with background `build` / `quick`
subagents. Both clients in the same change (desktop/web; mobile shows no tool or notice rows).

## What the screenshot shows today

```text
⣿ Build Subagent heading and root-back navigation   Background
  opencode/deepseek-v4.1-flash/high · 0.0s
▸ 5 cells, 9 thoughts, 3 tools, 3 edits, 1 read  +6 -4
⣿ Build Subagent heading and root-back navigation
  opencode/deepseek-v4.1-flash/high · 5m 10s
▸ 1 thought, 1 command
▸ context · Notification: sub agent finished · show details
▸ → Background Shell Done: 'cd nexus-tui && cargo test …' · show full o…
▸ Thought · **Delegating focused tests**
```

## 35.1 One-item rows show their own icon, not the fold triangle

- [x] When a row stands for **a single item** (one thought, one finished background shell), draw that item's icon
  where the `▸` fold glyph is now: thought `◇`, background shell `$` (the icons used inside an open group,
  `[glyphs]` in `interface/theme.toml`). The `→` prefix before "Background Shell Done" goes too, since the icon
  replaces it. The row still expands on click/Enter as before; only the glyph changes. Groups with more than one
  item keep `▸` / `▾`.
- [x] TUI: `nexus-tui/src/timeline.rs` (~657–662 background-shell detection, ~2595 the "· show full output" label,
  the group header that draws `glyphs.fold`). NSH: `nexus-nsh/src/ui/timeline.tsx` (`NOTICE` ~117, the notice row
  ~984–990, the group header).
- [x] Noticed in the same screenshot: thought titles show raw Markdown (`**Delegating focused tests**`). Strip the
  emphasis markers in the one-line title (or render them). Ask the user whether to include it in this change; default yes.

## 35.2 "Subagent done" instead of "context · Notification: sub agent finished"

Today Core adds `[Notification: sub agent finished]\n<report>` to the parent's context (`runtime.py` ~2854), and both
clients render it as `▸ context · Notification: sub agent finished · show details`. NSH already opens the full text
in a dialog (`timeline.tsx` ~984).

- [x] Recognise this notice and draw it as **`<icon> Subagent done`**, followed by the agent and title in muted text
  (`Subagent done · Build · Subagent heading and root-back navigation`), plus `failed` / `interrupted` in the error
  colour when the report says so. Icon: the subagent glyph (`⣿`, the same as the subagent start row) in the agent's
  colour. Other `context` notices keep today's look.
- [x] Core: put the child's id, agent and outcome on the notice as structured metadata (the message text stays exactly
  what the model gets), so clients don't parse the report text. Only for new notices; older ones fall back to parsing
  `Sub agent ses_… (agent build) …`.
- [x] **Click opens a modal:** title `Subagent done`, the agent + title + model + duration, and the **full notice text
  exactly as the model received it** (scrollable, selectable). Footer button **Open session** navigates to the child
  session (as clicking the subagent's start row does), plus `esc close`. Same modal in both clients; it replaces
  NSH's plain text dialog for this notice.

## Checks

- [x] A shared scenario in `interface/scenarios/` with a background subagent and its done notice, a single-thought row and
  a single background-shell row; TUI frame tests and NSH tests; matching screenshots in dark and light.
- [x] Docs: `interface/design-language.md` (timeline rows), both READMEs. No context change: the model still
  receives the same notice text.

## Verification handoff

The implementation agent was interrupted by provider errors before returning its final report. Independent review reran Core `tests/test_children.py tests/test_loop.py` (31 passed), TUI `--test timeline_notices` (1 passed), NSH `scenarios.test.tsx` (4 passed) and `thought.test.tsx` (2 passed), plus `git diff --check`. The reviewer inspected paired dark timeline and light modal captures with no clear parity defect, and confirmed mobile excludes notice rows in code; no device run was performed. Broader suite/build results recorded below are implementation notes, not independently rerun. NSH test execution also regenerated five unrelated `reports/section14/inspector/*.png` files; those are not plan 35 evidence.

## What landed (2026-11-11)

- **Core.** A stored message row carries an optional `notices` JSON array; it is never part of the provider message.
  `_deliver` attaches `{kind: "subagent", state, session_id, agent, title, model, effort, duration}` while the text the
  model receives stays byte-identical. `wait: false` delivery reaches the parent through the stored message, the
  in-memory inbox (`loop.Notice`, merged with any other notices in the same user message) and archived sessions.
  `/message`, message pages, the snapshot's selected messages, `message.added` and `session.notified` expose it.
- **Fallback.** Clients parse `Sub agent ses_… (agent build) finished|failed|was interrupted…` for messages stored
  before the metadata and fill title/model/duration from the child session they already have.
- **TUI.** A lone thought shows `◇` in place of the fold; one background shell shows `$` and the `→` prefix is gone;
  multi-item groups keep the fold. A subagent notice is `⣿ Subagent done · Agent · title` with the failure in the
  error colour. Enter opens `Subagent done` with agent/title/model/duration and the exact notice text, scrollable;
  `enter open session` opens the child and `esc` closes.
- **NSH.** The same row, with the diamond and shell SVG icons, and the same modal with an **Open session** button.
  The old plain text dialog is no longer used for this notice. Mobile is unchanged (no notice rows).
- Verified: Core `tests/test_children.py` (structured metadata + exact text) and `tests/test_loop.py` (metadata rides
  with merged notice text, never in the request). Full Core suite: 1,493 passed, 5 skipped, 1 pre-existing failure
  (`test_no_import_or_registry_coupling`; `nexus/server.py` already imports `.workers.registry` at HEAD). TUI
  `tests/timeline_notices.rs` from the shared scenario plus unit tests for emphasis stripping and old-notice parsing;
  `cargo test --lib` is 397 passed except the pre-existing `manifest::tests::settings_order_is_fixed` (it expects no
  `zmx` Settings section), and `cargo test --tests` is green except the known stale ZMX scenario (plan 6). `App::open_palette`
  became `pub` so the existing `tests/scenarios.rs` target compiles at all. NSH `scenarios.test.tsx` (row, modal,
  Open session) and `thought.test.tsx` pass; the web build passes; `npm run check` still reports only the pre-existing
  `composer.tsx` `TS`/`onMouseEnter` errors.
- Screenshots: `reports/35-timeline-notice-rows/` (`tui-{dark,light}.png` from a real seeded TUI; `nsh-{dark,light}.png`
  from the NSH web build on the same Core; `nsh-*-modal.png`; `tui-{dark,light}-modal.png` rendered by the TUI test
  harness from the shared scenario, because the real binary capture raced a scratch-session tool-sync notice). Mobile,
  phone layouts and live providers were not exercised.
