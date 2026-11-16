# Checks that need you (pre-release 2026-11, in progress)

Each line is one check with the exact command or steps. Nothing here was run by the agent. Scratch homes only, unless a
line says otherwise. Items from later phases are added as they land.

## Terminal and layout (hand checks)

- [ ] **Real Ghostty drag (plan 45).** `nexus` in Ghostty: open two session tabs (`ctrl+k`), drag a tab onto the body's
  right edge (the zone should light), release; drag a divider; press Esc mid-drag (cancels); Alt-drag a tab (copies).
  Also Ctrl+click a session or file row (opens to the side). Compare with `reports/45-tabs-and-splits/`.
- [ ] **Real Chrome drag (plan 45).** `nexus web`, the same drags, plus a touch-less trackpad drag across the sidebar.
- [ ] **Launch splash in Ghostty (plan 26).** Stop the daemon (`nexus stop`), then `nexus`: the logo animates from the first
  frame, `Starting Nexus Core…` shows after 5 s, the live screen replaces it. (`reports/26-launch-splash/` has the scratch run.)
- [ ] **Rail drag (backlog 9.12).** In `nexus` and `nexus web`, drag a project tile in the rail: a highlighted target, Esc
  or a drop outside cancels, a plain click still selects; also `alt+up` / `alt+down` on a project.

## Manager

- [ ] **Indicator colour by speaker with real voice (plan 28).** Start a Manager voice call; the wave is the listening colour
  while you talk, the speaking colour while it answers, a flat line when muted, still when disconnected
  (`reports/28-manager-indicator/` shows the frames).

## ZMX (paid CLI runs, not approved in this task)

- [ ] **Paid M0 spikes** (Claude Code 2.1.322 / agy 1.5.x, cheapest model, scratch `$NEXUS10_HOME`): rebuild an `api-request`
  and compare it with a local capture; `message.id` and parallel tool results; `--append-system-prompt`; `StopFailure`
  payload and permission wording under `dontAsk` / `auto`; `--mcp-config` with a Nexus server entry; `claude -p` vs
  interactive quota; hidden requests (`ai-title`); agy `--mode auto` and its soft denial; agy subagents; a real interrupt
  then `--resume`. List: `plans/zmx-design.md` section 6 (M0).
- [ ] **Vendor terms.** Ask Anthropic and Google in writing whether a local program driving the unmodified `claude` / `agy`
  under the user's own sign-in is allowed (`plans/06-zmx.md`, "Vendor terms").
