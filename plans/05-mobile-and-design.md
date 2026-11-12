# 5. Mobile client and the NSH look

Moved from PLAN.md on 2026-11-11 (5.1 is the old section 5, unchanged). 5.2 and 5.3 are the user's new requests of
2026-11-11 (their items 5 and 6, merged).

## 5.1 Mobile cut-down client: Sessions, Manager, Settings

Decision (user, 2026-11-06): mobile (the NSH phone build, iOS and any other mobile target) has **three tabs only:
Sessions, Manager and Settings**. A session view shows the session's status, the user's messages and the agent's replies
and nothing else: no tool groups, no context header. Everything else (files, editor, Git, inspector, terminal, browser,
projects overview and the other desktop surfaces) is desktop/web only. Adding anything beyond this needs the user's
approval. `AGENTS.md` is updated with this change.

Decision (user, 2026-11-09): **mobile gets all slash commands** in its composer (for example `/btw`, section 23).

Implemented in the working tree for iPhone and iPad: three tabs, reduced timeline, compact waiting cards and mobile-scoped Settings. Four pure rule tests and NSH type check pass. Runtime phone screenshots remain unverified: no simulator devices are installed, and the discovered physical iPhone is unavailable.

- [~] **Navigation:** implemented; native runtime verification remains open. A three-tab bar (Sessions, Manager, Settings) replaces the current phone pages; define the Manager
  tab (the header indicator and dropdown become a full tab: caption, cards, typed input, voice; plus the read-only
  backend-session view only if the user wants it).
- [~] **Session view:** filtering and compact cards implemented; verified headless and at 390 px in a browser (2026-11-11), simulator open. Status, user messages, agent replies, composer. **Mobile can approve and answer everything a
  desktop client can** (user, 2026-11-06): allow once/always/deny, answer a question (options and free text), choose
  among offered choices, steer/queue/interrupt. It shows less, because the screen is small: a compact card with the
  command or question and the choices, not the full diff, parameter table or tool group. Define the compact card once
  and test it at 390 px. Allowed, since an answer is part of "status"; the other tool detail stays out.
- [x] **Settings:** mobile scope in the manifest selects the sections/rows that apply (Providers sign-in, Appearance, Notifications, Voice, About; hide Agents,
  Tools, Skills, MCP, Hooks, Files/Editor, Terminal). List them in the manifest under a `mobile` scope.
- [x] **Remove** desktop capabilities from the iPhone/iPad build, including command and session-menu guards (4 rule tests pass). Excluded: Files tab, editor, Git, inspector, terminal, browser, overview, project rail
  management, row menus that reach them. Make them unreachable from the palette and shortcuts too.
- [~] Phone polish on the three tabs (2026-11-11): keyboard avoidance (shell pads by `keyboardFrame()`), 44 px targets, safe areas on bars, sheets and drawer, gestures (long press, pull to refresh, edge swipe), Settings as pushed pages and the Connect copy are done and verified headless and in a 390 px browser. Open: the iPad decision, composer-selector targets under 44 px (the desktop composer row is hidden on a phone but not audited), landscape insets, Dynamic Type, and everything on a device.
- [ ] Native mic for the Manager (section 3) so the Manager tab can talk.
- [~] Tests on the phone idiom at 390 px (`phone-redesign.test.tsx`, `mobile.test.ts`, `phone-session-controls.test.tsx`) and a phone screenshot of each tab, dark and light, are done ([reports/mobile-2026-11-11](../reports/mobile-2026-11-11/)); `nexus-nsh/README.md` and docs updated. **Open:** a real simulator run and a signed device build (see the `nexus-ios-device` skill).
- [ ] **Remote iPhone install (from 2026-11-09):** Capture the paired iPhone's Bonjour announcement with Xcode Tailscale Bridge
  while phone and Mac are on the same local network, then verify a signed Nexus
  update from a different network over Tailscale. Remote diagnostics/build passed,
  but capture was blocked by lack of local-network adjacency; no install occurred.
  Checklist and signing caveats: [NSH remote updates](../nexus-nsh/README.md#remote-iphone-updates-over-tailscale-pending-verification).


## 5.2 Mobile redesign (user, 2026-11-11)

The user asked for a better mobile interface. Inspiration screenshots were shared in the 2026-11-11 conversation:
a session drawer with a project switcher, a phone pairing screen, a settings server card and a compact approval card.
They are references for **quality and ergonomics only**. Do not copy another product's name, logo, wordmark, layout
one to one or copy text. Nexus keeps its own design language (`interface/design-language.md`, `theme.toml`).

The three tabs stay: **Sessions, Manager, Settings**. New on top of 5.1:

- [x] **Session drawer (swipe from the left edge).** In an open session, an edge swipe (and a header button) reveals
  the session list over the conversation, which slides aside and dims. Tapping a row opens that session and closes the
  drawer. Rows show the title, a project colour dot, the status (spinner while busy, a `Needs your input` line for a
  pending approval or question) and a relative time. The top of the drawer has the **project switcher** (current
  project name + chevron → a list of projects and "All projects") and a search button that opens the search modal
  focused (see [29](done/29-session-commands.md)). Reuse `EdgeDrawer` in `nexus-nsh/src/ui/overlays.tsx`; the gesture must not fight
  iOS back-swipe or horizontal scrolling inside code blocks.
- [x] **Sessions tab** is the same list full screen: project switcher title, search button, then sections. Proposed
  sections: *Needs you* (waiting sessions, with a count), *Running*, *Recent*. Long-press for the row menu (rename,
  archive). Pull to refresh.
- [x] **Session view:** a compact header (title, project as a subtitle, a status ring/spinner, overflow menu with
  rename / archive). Messages in a readable column. The approval/question card follows 5.1 (compact card) and should
  read like the reference: the command or question, primary choices as two big buttons (`Allow once`, `Always allow`),
  then a free-text line "Tell it what to do instead" with `Deny`. 44 px targets, keyboard avoidance.
- [x] **Settings tab:** a grouped list with drill-in pages (iOS style). A **connection card** at the top: the Core's
  host name, address and transport (LAN / Tailscale), a live latency figure and a small health trace. Then
  Appearance, Notifications, Voice, Providers, About.
- [x] **Connect / pairing screen** (first run and when disconnected): a short numbered setup (install Nexus on the
  computer; run a pairing command; scan its QR code or paste the link it prints), plus "Enter a server manually".
  Recent Cores list. Clear "cannot reach (Local Network permission?)" error and Retry. This absorbs the Connect flow
  item in [09-backlog.md](09-backlog.md) 9.6.

Built 2026-11-11 in `nexus-nsh/src/ui/mobile/` (headless tests and 390 px browser screenshots; simulator and device unverified; no in-app QR scanner or `nexus://` URL-scheme handler yet, so the link is pasted; the drawer dims but does not slide the conversation aside). Search is the always-visible field in the list and drawer rather than a separate button.

Questions for the user before building. **All four defaults confirmed by the user on 2026-11-11 (recorded in [decisions.md](decisions.md)); `nexus pair` is implemented:**

| Question | Default |
| --- | --- |
| A `Used 22 tools · 1 failed` summary line between replies would add tool information to mobile, which AGENTS.md excludes | **Leave it out** unless the user approves it; it would then need a rule change in AGENTS.md |
| Pinned sessions and a "Catch up" (unread digest) row: Nexus has neither concept today | Leave out; *Needs you* covers the urgent part |
| Pairing needs a desktop command that prints a QR code / link with the Core address and token (e.g. `nexus pair`) | Add `nexus pair` to the Core CLI (prints address, token, QR in the terminal); no relay service |
| Project switcher on mobile: does it filter the list or also set the project for new sessions? | Both, like the desktop rail |

Checks: phone idiom at 390 px in the NSH browser harness with `platform.idiom` stubbed; screenshots of each tab, the
drawer open and the approval card, dark and light; then a simulator and a signed device build (`nexus-ios-device`
skill). Update `nexus-nsh/README.md`.

## 5.3 NSH web and macOS look (user, 2026-11-11; merged with 5.2)

The user asked for better-looking NSH web and macOS interfaces, and to do it together with the mobile work so the
three share one visual language (type scale, spacing, radii, list rows, cards, buttons, status dots, the approval
card).

- [ ] **Audit first:** screenshots of NSH web at 1024 / 1440 px and the macOS app beside the TUI at 120×40 / 160×48,
  dark and light, across the main surfaces (sessions sidebar, session view, composer, waiting cards, modals,
  Settings). List what looks unfinished: spacing, alignment, weights, contrast, hover/pressed states, borders.
- [ ] **One component pass** in `nexus-nsh/src/ui/kit.tsx` and the theme tokens: list row, card, primary/secondary
  button, segmented control, status dot/ring, section header. Mobile 5.2 uses the same components.
- [ ] **Parity rule:** layout, wording, order and interactions still match the TUI. Purely rendering-level finish
  (radius, font rendering, shadows, translucency) follows the existing approved NSH difference (rounded surfaces,
  2026-11-06 ledger). Anything else that the TUI cannot or should not copy needs the user's approval and a ledger row.
  Where a change is also right for the TUI (spacing, wording, grouping), land it there in the same change.
- [ ] Verify with Playwright screenshots (web) and the macOS app; before/after pairs saved under
  `reports/nsh-look-2026-11/`.
