# 35. Timeline: icons for single items, "Subagent done" rows

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

- [ ] When a row stands for **a single item** (one thought, one finished background shell), draw that item's icon
  where the `▸` fold glyph is now: thought `◇`, background shell `$` (the icons used inside an open group,
  `[glyphs]` in `interface/theme.toml`). The `→` prefix before "Background Shell Done" goes too, since the icon
  replaces it. The row still expands on click/Enter as before; only the glyph changes. Groups with more than one
  item keep `▸` / `▾`.
- [ ] TUI: `nexus-tui/src/timeline.rs` (~657–662 background-shell detection, ~2595 the "· show full output" label,
  the group header that draws `glyphs.fold`). NSH: `nexus-nsh/src/ui/timeline.tsx` (`NOTICE` ~117, the notice row
  ~984–990, the group header).
- [ ] Noticed in the same screenshot: thought titles show raw Markdown (`**Delegating focused tests**`). Strip the
  emphasis markers in the one-line title (or render them). Ask the user whether to include it in this change; default yes.

## 35.2 "Subagent done" instead of "context · Notification: sub agent finished"

Today Core adds `[Notification: sub agent finished]\n<report>` to the parent's context (`runtime.py` ~2854), and both
clients render it as `▸ context · Notification: sub agent finished · show details`. NSH already opens the full text
in a dialog (`timeline.tsx` ~984).

- [ ] Recognise this notice and draw it as **`<icon> Subagent done`**, followed by the agent and title in muted text
  (`Subagent done · Build · Subagent heading and root-back navigation`), plus `failed` / `interrupted` in the error
  colour when the report says so. Icon: the subagent glyph (`⣿`, the same as the subagent start row) in the agent's
  colour. Other `context` notices keep today's look.
- [ ] Core: put the child's id, agent and outcome on the notice as structured metadata (the message text stays exactly
  what the model gets), so clients don't parse the report text. Only for new notices; older ones fall back to parsing
  `Sub agent ses_… (agent build) …`.
- [ ] **Click opens a modal:** title `Subagent done`, the agent + title + model + duration, and the **full notice text
  exactly as the model received it** (scrollable, selectable). Footer button **Open session** navigates to the child
  session (as clicking the subagent's start row does), plus `esc close`. Same modal in both clients; it replaces
  NSH's plain text dialog for this notice.

## Checks

- [ ] A shared scenario in `interface/scenarios/` with a background subagent and its done notice, a single-thought row and
  a single background-shell row; TUI frame tests and NSH tests; matching screenshots in dark and light.
- [ ] Docs: `interface/design-language.md` (timeline rows), both READMEs. No context change: the model still
  receives the same notice text.
