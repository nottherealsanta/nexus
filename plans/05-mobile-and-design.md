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
- [~] **Session view:** filtering and compact cards implemented; 390 px runtime verification remains open. Status, user messages, agent replies, composer. **Mobile can approve and answer everything a
  desktop client can** (user, 2026-11-06): allow once/always/deny, answer a question (options and free text), choose
  among offered choices, steer/queue/interrupt. It shows less, because the screen is small: a compact card with the
  command or question and the choices, not the full diff, parameter table or tool group. Define the compact card once
  and test it at 390 px. Allowed, since an answer is part of "status"; the other tool detail stays out.
- [x] **Settings:** mobile scope in the manifest selects the sections/rows that apply (Providers sign-in, Appearance, Notifications, Voice, About; hide Agents,
  Tools, Skills, MCP, Hooks, Files/Editor, Terminal). List them in the manifest under a `mobile` scope.
- [x] **Remove** desktop capabilities from the iPhone/iPad build, including command and session-menu guards (4 rule tests pass). Excluded: Files tab, editor, Git, inspector, terminal, browser, overview, project rail
  management, row menus that reach them. Make them unreachable from the palette and shortcuts too.
- [ ] Phone polish already listed in the archive (8.5 keyboard avoidance, 44 px targets, safe areas, gestures,
  Settings as pushed pages, Connect copy for a tailnet host, iPad decision); do the ones on the three tabs.
  Partial 2026-11-12: the phone shell reserves the keyboard inset; its tabs and composer stop adding bottom safe-area
  padding while the keyboard is open. A dedicated back button dismisses the keyboard and returns to Sessions without
  discarding the draft, and a keyboard-dismiss button is shown while editing. Phone-idiom navigation test passes;
  software-keyboard geometry and touch behavior still need physical verification.
- [ ] Native mic for the Manager (section 3) so the Manager tab can talk.
  Partial 2026-11-12: native PCM conversion and 20 ms framing for GPT Live has unit tests in
  `nexus-nsh/native/src/live_pcm.rs`. A bounded native WebSocket transport in `live_socket.rs` has loopback tests for
  PCM, mute, disconnect and token-safe errors. Neither is connected to the Manager or device yet. Full-duplex iOS audio,
  playback, interruption/cleanup, Manager integration and real-device verification remain open.
  The partial native crate also passes `cargo check --target aarch64-apple-ios`; this does not exercise audio hardware.
- [ ] Tests on the phone idiom at 390 px, a phone screenshot of each tab, a real simulator run and a signed device build
  (see the `nexus-ios-device` skill); update `nexus-nsh/README.md` and `docs/`.
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

- [ ] **Session drawer (swipe from the left edge).** In an open session, an edge swipe (and a header button) reveals
  the session list over the conversation, which slides aside and dims. Tapping a row opens that session and closes the
  drawer. Rows show the title, a project colour dot, the status (spinner while busy, a `Needs your input` line for a
  pending approval or question) and a relative time. The top of the drawer has the **project switcher** (current
  project name + chevron → a list of projects and "All projects") and a search button that opens the search modal
  focused (see [29](done/29-session-commands.md)). Reuse `EdgeDrawer` in `nexus-nsh/src/ui/overlays.tsx`; the gesture must not fight
  iOS back-swipe or horizontal scrolling inside code blocks.
  Partial 2026-11-12: the header opens a Sessions drawer with the shared list and project selector; choosing a session
  closes it, and All sessions returns to the tab. The phone project picker includes All projects and selecting a project
  sets the project scope. Phone-idiom route tests pass. Edge swipe, sliding motion and
  phone-device verification remain open.
- [ ] **Sessions tab** is the same list full screen: project switcher title, search button, then sections. Proposed
  sections: *Needs you* (waiting sessions, with a count), *Running*, *Recent*. Long-press for the row menu (rename,
  archive). Pull to refresh.
  Partial 2026-11-12: the phone list groups sessions into these sections, prioritizes waiting over running, and shows
  waiting/running indicators and relative times. All-projects filtering works independently of the desktop rail.
  Grouping rule tests pass; a signed build launched on the physical iPhone and showed Running and Recent with live
  sessions on 2026-11-12. Long-press, pull-to-refresh and the Needs you section on-device remain unverified or unimplemented.
  Later 11-12: refined phone list typography, spacing and selection, removed duplicate scope chips in favor of the
  project picker, and installed the updated build. The device capture showed the empty state; populated rows still need
  a fresh visual check.
- [ ] **Session view:** a compact header (title, project as a subtitle, a status ring/spinner, overflow menu with
  rename / archive). Messages in a readable column. The approval/question card follows 5.1 (compact card) and should
  read like the reference: the command or question, primary choices as two big buttons (`Allow once`, `Always allow`),
  then a free-text line "Tell it what to do instead" with `Deny`. 44 px targets, keyboard avoidance.
  Partial 2026-11-12: the header shows the project alongside the existing status word. The remaining header and card
  work and hardware checks remain open. A further pass gives approval choices larger two-column phone targets while
  retaining all four existing decisions; a phone-idiom test covers their presence, but device card capture remains open.
- [ ] **Settings tab:** a grouped list with drill-in pages (iOS style). A **connection card** at the top: the Core's
  host name, address and transport (LAN / Tailscale), a live latency figure and a small health trace. Then
  Appearance, Notifications, Voice, Providers, About.
  Partial 2026-11-12: phone Settings now has a connection card with the saved address and actual connection state,
  plus a grouped list of the existing mobile sections with drill-in/back navigation. Provider details are mounted on
  phone. Latency, health trace, transport identification and device visual checks remain open.
- [ ] **Connect / pairing screen** (first run and when disconnected): a short numbered setup (install Nexus on the
  computer; run a pairing command; scan its QR code or paste the link it prints), plus "Enter a server manually".
  Recent Cores list. Clear "cannot reach (Local Network permission?)" error and Retry. This absorbs the Connect flow
  item in [09-backlog.md](09-backlog.md) 9.6.
  Partial 2026-11-12: manual connect shows two setup steps, LAN/Tailscale guidance, a trusted-network warning and
  permission troubleshooting. Pairing, QR scanning and recent Cores are not implemented.

Questions for the user before building. **All four defaults confirmed by the user on 2026-11-11:**

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

- [x] **Audit first (NSH web and TUI done 11-11; macOS app open):** screenshots of NSH web at 1024 / 1440 px and the macOS app beside the TUI at 120×40 / 160×48,
  dark and light, across the main surfaces (sessions sidebar, session view, composer, waiting cards, modals,
  Settings). List what looks unfinished: spacing, alignment, weights, contrast, hover/pressed states, borders.
- [~] **One component pass (first pass landed 11-11: `Btn` hairline, `Seg` spacing, approval table label column and TUI-matching key order, Settings label column; list row, card, status dot and section header reviewed, unchanged)** in `nexus-nsh/src/ui/kit.tsx` and the theme tokens: list row, card, primary/secondary
  button, segmented control, status dot/ring, section header. Mobile 5.2 uses the same components.
- [ ] **Parity rule:** layout, wording, order and interactions still match the TUI. Purely rendering-level finish
  (radius, font rendering, shadows, translucency) follows the existing approved NSH difference (rounded surfaces,
  2026-11-06 ledger). Anything else that the TUI cannot or should not copy needs the user's approval and a ledger row.
  Where a change is also right for the TUI (spacing, wording, grouping), land it there in the same change.
- [~] Verify with Playwright screenshots (web done, 1024/1440 dark/light; macOS app not attempted: no non-interactive launch) and the macOS app; before/after pairs saved under
  `reports/nsh-look-2026-11/`.

Audit list, open items (tab clip without ellipsis, light-theme selection contrast, Settings notes vs TUI one-line rows, palette disabled reasons) and before/after pairs: [reports/nsh-look-2026-11/README.md](../reports/nsh-look-2026-11/README.md).
