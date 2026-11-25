# 5. Mobile client and the NSH look

Moved from PLAN.md on 2026-11-11 (5.1 is the old section 5, unchanged). 5.2 and 5.3 are the user's new requests of
2026-11-11 (their items 5 and 6, merged).

## 5.1 Mobile cut-down client: Sessions, Manager, Settings

Decision (user, 2026-11-06): mobile (the NSH phone build, iOS and any other mobile target) has **three tabs only:
Sessions, Manager and Settings**. A session view shows the session's status, the user's messages, the agent's replies
and one compact summary row per tool group (approved 2026-11-23), and nothing else: no context header and no tool
detail. Everything else (files, editor, Git, inspector, terminal, browser,
projects overview and the other desktop surfaces) is desktop/web only. Adding anything beyond this needs the user's
approval. `AGENTS.md` is updated with this change.

Decision (user, 2026-11-09): **mobile gets all slash commands** in its composer (for example `/btw`, section 23).

Decision (user, 2026-11-12): **the iPad shows the desktop/web client, not the phone layout.** There is no iPad app and none is planned. Only `platform.idiom === "phone"` selects the cut-down client (`mobileClient` in `nexus-nsh/src/mobile.ts`); iPad and iPad-in-browser get the desktop layout with every desktop surface. A browser on an iPad is just the web build. If an iOS build ever runs on an iPad it is the desktop client too, so the iPhone-only guards (`desktopOnly`, Settings scope) do not apply to it. Touch targets still follow `touch` (phone or pad idiom).

Implemented in the working tree for iPhone: three tabs, reduced timeline with tool-group summary rows, compact waiting cards and mobile-scoped Settings. Four pure rule tests and NSH type check pass. Runtime phone screenshots remain unverified: no simulator devices are installed, and the discovered physical iPhone is unavailable.

- [~] **Navigation:** implemented; native runtime verification remains open. A three-tab bar (Sessions, Manager, Settings) replaces the current phone pages; a conversation hides it and returns through Back (5.2, user 2026-11-23). Define the Manager
  tab (the header indicator and dropdown become a full tab: caption, cards, typed input, voice; plus the read-only
  backend-session view only if the user wants it).
- [~] **Session view:** filtering and compact cards implemented; verified headless and at 390 px in a browser (2026-11-11), simulator open. Status, user messages, agent replies, composer. **Mobile can approve and answer everything a
  desktop client can** (user, 2026-11-06): allow once/always/deny, answer a question (options and free text), choose
  among offered choices, steer/queue/interrupt. It shows less, because the screen is small: a compact card with the
  command or question and the choices, not the full diff or parameter table. A tool group is one compact,
  always-collapsed, non-expandable summary row (`1 thought, 2 commands`, running/waiting/done, `+N −M`), with no
  members or tool detail (user, 2026-11-23). Define the compact card once and test it at 390 px. Allowed, since an
  answer is part of "status"; the other tool detail stays out.
- [x] **Settings:** mobile scope in the manifest selects the sections/rows that apply (Providers sign-in, Appearance, Notifications, Voice, About; hide Agents,
  Tools, Skills, MCP, Hooks, Files/Editor, Terminal). List them in the manifest under a `mobile` scope.
- [x] **Remove** desktop capabilities from the iPhone build, including command and session-menu guards (4 rule tests pass). Excluded: Files tab, editor, Git, inspector, terminal, browser, overview, project rail
  management, row menus that reach them. Make them unreachable from the palette and shortcuts too.
- [~] Phone polish on the three tabs (2026-11-11): keyboard avoidance (shell pads by `keyboardFrame()`), 44 px targets, safe areas on bars, sheets and drawer, gestures (long press, pull to refresh, edge swipe), Settings as pushed pages and the Connect copy are done and verified headless and in a 390 px browser. Decided 2026-11-12: iPad is desktop/web. Open: Dynamic Type and everything on a device. Done 2026-11-12: the composer selectors are 44 px on a phone and the shell pads the left/right safe-area insets for landscape (headless tests pass; not seen on a device).
- [ ] Native mic for the Manager (section 3) so the Manager tab can talk.
  Partial 2026-11-12: native PCM conversion and 20 ms framing for GPT Live has unit tests in
  `nexus-nsh/native/src/live_pcm.rs`. A bounded native WebSocket transport in `live_socket.rs` has loopback tests for
  PCM, mute, disconnect and token-safe errors. Neither is connected to the Manager or device yet. Full-duplex iOS audio,
  playback, interruption/cleanup, Manager integration and real-device verification remain open.
  The partial native crate also passes `cargo check --target aarch64-apple-ios`; this does not exercise audio hardware.
- [~] Tests on the phone idiom at 390 px (`phone-redesign.test.tsx`, `mobile.test.ts`, `phone-session-controls.test.tsx`) and a phone screenshot of each tab, dark and light, are done ([reports/mobile-2026-11-11](../reports/mobile-2026-11-11/)); `nexus-nsh/README.md` and docs updated. **Open:** a real simulator run and a signed device build (see the `nexus-ios-device` skill).
  Fixed 2026-11-15: the user message's turn meta showed `#1 · Invalid Date` when Core's `created` was missing or
  unparsable. NSH's `clock` now omits an unusable time (as the TUI's `local_clock` already did) and the meta, handoff
  card and turn footer drop the empty part, so the row reads `#1`. Both the phone and the desktop timeline use the same
  row builder, so the desktop path is covered too. Test: `timeline-visual-parity.test.tsx` ("a missing or unparsable
  turn time is left out…").
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

- [x] **Session drawer (swipe from the left edge).** In an open session, an edge swipe (and, until the 2026-11-23 Back
  control replaced it, a header button) reveals
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
  Partial 2026-11-12: manual connect shows two setup steps, LAN/Tailscale guidance, a trusted-network warning and
  permission troubleshooting. **Corrected 2026-11-16 (the text above was stale):** pairing and recent Cores exist:
  `nexus pair` prints the address, token and a terminal QR code (`nexus-core/nexus/cli.py`, `pair`); the Connect screen
  parses `nexus://pair?url=&token=&name=` links and bare addresses (`parsePairing`, `nexus-nsh/src/mobile.ts`) and keeps
  the five most recent Cores (`rememberCore`, `ui/mobile/connect.tsx`, `nexus.recent-cores`). **Still not implemented:**
  the in-app QR scanner and the `nexus://` URL-scheme handler (the link is pasted); both need a device (carried over).

Polish 2026-11-12 (user: "make mobile design better"): the phone reads in Google Sans through a `--font-ui` token (`TS`/`T` roles in `kit.tsx`; code and mono text keep Monaspace), rounded surfaces (2xl/3xl cards, composer and user messages; xl buttons; round icon buttons and send), assistant replies as full-width prose at 16/25, a short composer placeholder (`Message`, `Steer or queue a message…`), the Working row left-aligned under the composer. Screenshots: [reports/mobile-2026-11-12](../reports/mobile-2026-11-12/). Not verified on a device. The 2026-11-23 pass below supersedes the phone's 16/25 prose and the below-composer Working row.

Built 2026-11-11 in `nexus-nsh/src/ui/mobile/` (headless tests and 390 px browser screenshots; simulator and device unverified; no in-app QR scanner or `nexus://` URL-scheme handler yet, so the link is pasted; the drawer dims; 2026-11-12 the conversation now slides aside by the drawer width, headless tests pass, not seen on a device). Search is the always-visible field in the list and drawer rather than a separate button.

Conversation navigation (user, 2026-11-23, superseding the same day's decision to keep the tabs visible): the root tabs stay **Sessions, Manager and Settings**; a conversation hides the tab bar and its header starts with an icon-only iOS-style back chevron whose accessible name is **Back to Sessions**. Back keeps the viewed session and its draft, returns to the full Sessions list, and a tap reopens the session; Manager and Settings are then one tap away. The session drawer and its left-edge swipe stay as they were (the drawer no longer has a header button). While the bar is hidden the shell carries the home-indicator inset, and the open keyboard replaces it. Implemented in `shell.tsx` and `state.ts`; pure rules `phoneTabBarVisible` and `phoneShellBottomInset` in `src/mobile.ts`. Tests: `phone-redesign` (tab order, hidden bar, Back, draft kept, safe area) and `mobile.test.ts`; 390 px screenshots checked ([reports/mobile-2026-11-23](../reports/mobile-2026-11-23/)), no device.

Phone conversation polish (user, 2026-11-23): the header is a 17/22 title with the project and live status beneath it. While a turn runs that line says `Working · 11s` and the seconds tick every second from the latest user message's start; waiting keeps its warning state. The bottom Working row and the activity bar are removed on the phone only. The composer is two compact rows: a full-width input (16/22) above a control row with the agent/model selectors on the left (13/20, truncating long names) and mic/send on the right (44 pt hit targets, smaller 32 pt visible circles), with tightened card padding; pending messages, dictation, approval, queue/steer, long-press send and drafts are unchanged. Assistant prose and user messages use the bundled Google Sans at 16/23 and 16/22 with tighter paragraph and bubble spacing; fenced code, inline code, commands and tool arguments remain Monospace. A phone user message shows only its text by default; a long press reveals its delivery tag, turn number, time, Copy and Fold controls (`revealedBand`, `ui/timeline.tsx`), while its accessible label keeps the full text and detail and its hint names the gesture. Long messages keep the Show full control, and the sticky user-message bar no longer renders on the phone. The phone's tool summary rows are slightly shorter but keep their labels, counts, diff and accessible description. Tests: `phone-redesign` (compact composer, no bottom status, live header seconds, prose/code scale, long names, rapid-send guard, hidden/revealed message details and actions, full long message, no sticky phone bar, approval plus safe area) plus `phone-session-controls`; desktop tests `timeline-layout`, `void-second-pass` and `session-controls` pass unchanged, while `timeline-scroll` has the same pre-existing `switching away and back restores where the reader was` failure at `HEAD` (13 of 14 pass). 390 px idle, waiting/busy, focused, revealed user details and drawer captures are in the same report; no device.

Tool summaries (user, 2026-11-23): the phone timeline keeps tool groups in their original order as one raised rounded
row each, always collapsed and non-expandable: a status badge (spinner while working, `◆` waiting, `✓` done), the
group's counts from every member (`phoneActivityStatus`, `activityCountsText` in `nexus-nsh/src/mobile.ts`;
`GroupCount` is carried before a running group truncates its members), its diff when it has one, and a trailing
`Working` / `Waiting` / `Done` (or `N denied`). No command, path, output, diff detail or member row is rendered, and
an approval keeps its own card above the composer. The spinner falls back to a still glyph under reduced motion.
Desktop/web keep the expandable group unchanged. Tests: `phone-redesign.test.tsx` (order, no members, running counts
past truncation, waiting and its approval) and `mobile.test.ts`; 390 px dark and light browser screenshots were checked,
not a device.

Questions for the user before building. **All four defaults confirmed by the user on 2026-11-11 (recorded in [decisions.md](decisions.md)); `nexus pair` is implemented:**

| Question | Default |
| --- | --- |
| A `Used 22 tools · 1 failed` summary line in the sessions list would add tool information mobile does not otherwise show | **Leave it out**; the conversation's tool-group summary rows are the approved exception (2026-11-23) |
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
  2026-11-15: audit item 7 (light-theme selection contrast) fixed with tokens only, in both clients:
  `interactive.selection` `#f0e3d8` → `#e8d0b9` and `interactive.settings_selection` `#ddddd9` → `#d9d9d4` (with the
  256-colour selection fallback 224 → 223). A selected segment or row is now a warm sand band that stands off the
  panel, sidebar and raised surfaces, while focused Settings rows keep the neutral gray band; no client difference.
  NSH 1024 light Settings and palette, and TUI 120×40 / 160×48 light Settings, completion and text frames, are in
  `reports/nsh-look-2026-11/` (`*-light-selection-*`, `tui-*-light-*-after`).

## 5.5 Neutral iOS redesign (user, 2026-11-24)

The user asked to "design the iOS app well", then: light and dark, **no orange accent, keep it neutral**, the composer
should look good, and the Manager's wave smaller. The canvas is the private design artifact
[Nexus iOS Redesign](https://claude.ai/artifact/8Dix7oEjGeN6YPw2bGRzj3) (Sessions, composer states, waiting on you,
working, Manager, Settings, Connect; dark and light rows). The user approved it as drawn ("apply this"), leaving the
QR scanner out. Phone only; desktop and web are unchanged.

- [x] **Neutral palette.** The phone's primary action is the text colour (`bg-foreground` / `text-background`); colour
  is kept for status (amber waiting, blue working, red deny and errors, green diffs). Two NSH-only tokens in
  `nexus-nsh/theme-void.toml`: `surface.phone_card` (dark `#1c1c1c`, light `#ffffff`) and `surface.phone_control`
  (dark `#2a2a2a`, light `#efefec`), as `bg-phone-card` / `bg-phone-control`. `Btn` on a phone: primary is a neutral
  pill, default a quiet fill.
- [x] **Shell:** large 34 pt titles on the canvas colour (no chrome band or hairline), an iOS chevron back
  (`chevron-left`), the status mark in the header subtitle (the status ring is gone), a neutral tab bar with a
  waiting-count badge on Sessions.
- [x] **Sessions:** a "Sessions" large title (the wordmark is gone) over a plain project switcher and a 38 pt search
  field; uppercase muted section labels; rows on the phone card. A Needs-you row says `Wants to run <tool>`, shows the
  compact command and offers **Open** and **Allow once** (approved). The project name shows only in All projects. No
  last-reply line: Core sends no reply preview for the list.
- [x] **Waiting on you:** the card on the phone card, `Run this command?` for bash (`Allow <tool>?` otherwise) with the
  tool name; no timer; Deny, Always allow and Allow once as three pills. The separate note field is gone: the
  composer is "what to do instead" (approved).
- [x] **Composer:** see `nexus-nsh/README.md`: neutral pills, round send that fills when there is a draft, Stop while
  running with an empty draft. Effort is left off the phone's control row.
- [x] **Other session waiting** is a pill (◆ text ›) instead of the info strip; a tap goes there (approved).
- [x] **Manager tab:** canvas colour, a 22/30 caption, the card on the phone card, and a composer-style input row with
  round Mute / Talk / Connect. The wave stays the small one in the title. **Opening the tab shows the panel only**
  (`showManagerPanel`): it does not start the orb loop, a call or the voice (user, 2026-11-24).
- [x] **Settings:** a simpler connection card (host, address · transport, Core version, Connected, a small neutral
  trace and latency, a Change Core row); uppercase group labels; 50 pt rows on the phone card.
- [x] **Connect:** neutral; a mono `nexus` mark, three steps, a primary **Paste pairing link**, the link field with
  Connect, manual entry and recent Cores. No QR scanner (user: leave it).
- [x] **Navigation (user, 2026-11-24):** in a conversation a left-edge swipe goes straight back to Sessions; the
  session drawer is removed from the conversation. This supersedes 5.2's drawer.
- [x] **Subagents (user, 2026-11-24: "have subagents also be visible in the app").** A subagent call is one card in the
  parent's timeline, in order (`PhoneChildRow` in `ui/mobile/activity.tsx`; `phoneRow` keeps `child` rows): the
  agent in small caps, the task title, a status badge and word (Working with a spinner, Needs you, Failed, Done), the
  model and running time, `background` for a background start, and a chevron. Tapping opens the child session. The
  child's header says `Subagent of <parent>` in place of the project; Back and the left-edge swipe return to the parent
  conversation (`goBack`, `phoneParent` in `ui/mobile/state.ts`), and from a root to Sessions. A root's Sessions row
  adds `N subagents · M running` (`subagentsText`); children are never root rows, and a child's approval already
  raises its root to Needs you. No tool detail from inside the child is shown on the parent. Tests: two in
  `phone-redesign.test.tsx` (card, open, header, back and swipe to the parent, the row count).
- [x] **Session page layout (user, 2026-11-24):** every row on the phone's session page shares a 28 px left gutter;
  text starts at its edge and status icons sit inside it. Tool groups and subagents have no card or border: a status
  badge in the gutter, the words on the text edge. The user message is more prominent: a filled bubble on the new
  `surface.phone_user` token (dark `#2e2f31`, light `#e6e6e1`), 18 px radius, medium-weight text. Composer pills shrink
  and truncate before the mic (the agent pill keeps its width up to 132 px). Checked in 390 px web captures.
- Tests: `phone-redesign.test.tsx` (31) and `phone-session-controls.test.tsx` (7) pass headless, including quick allow
  from the list, deny through the composer, Stop while running, the back swipe and the Manager tab making no Manager
  request. Installed on the paired iPhone with the `nexus-ios-device` recipe and checked in a device screenshot.

## 5.4 Redesign pass: web, macOS and phone (user, 2026-11-12)

The user asked for a redesign of NSH web, macOS and the phone: "70–80% there, polish to finish", taking cues from the
TUI. The old "Nexus Void Design" mockup artifact was removed; the new mockup is the private canvas
[Nexus NSH Redesign](https://claude.ai/artifact/24HpRYp1i9R7JW6YSoGxqf) (session, file picker, palette, composer
states, type rules, phone Sessions / conversation / Manager). Awaiting the user's review before implementation.

- [ ] **Sans by default.** Interface text is Google Sans everywhere (tabs, sidebar, rail, palette, pickers, Settings,
  modals, composer, prose, tool labels, turn footer). Monaspace only for code, diffs, shell commands and output, file
  paths and tool arguments, `@` mentions in the draft, the path strip, tree/braille glyphs and the wordmark. Today
  `font-mono` is spread across settings.tsx (12), composer.tsx (6), newsession.tsx (5), timeline/resources/
  project-mcp/overlays (4 each) and others. **2026-11-16 re-count:** 14 uses remain in `src/ui`, all code-like (the
  composer's path strips, shell output and commands in resources, the MCP file paths and commands, the inspector's lines,
  the splash wordmark, the Connect screen's field, a footer marker); the interface text runs through `--font-ui`.
- [ ] **One desktop type scale:** title 16/24, prose 15/24, body/UI 14/22, secondary 13/20, meta 12/16, section 11/16
  caps. Picker and palette rows are 14 px (the completion rows are 16 px today).
- [ ] **Pickers on top:** the `@` file and slash completion floats as a layer over the timeline, anchored to the
  composer's top edge, instead of sitting in flow and pushing the conversation up. Same change in the TUI (parity).
- [ ] **Composer:** the TUI soft card: text, then one control row (agent in its colour · model · effort … meter, mic,
  send/stop); the text area is one line high until the draft wraps; Working on the line below the card,
  right-aligned as in the TUI; path strip below in mono.
- [ ] **Phone:** the `nexus` wordmark in Monaspace at the top of Sessions; the tab bar gets 10 px more top padding.
- [ ] Verify with the screenshot skills (TUI and NSH web, dark and light) and record any client difference in the ledger.

Audit list, open items (tab clip without ellipsis, Settings notes vs TUI one-line rows, palette disabled reasons) and
before/after pairs: [reports/nsh-look-2026-11/README.md](../reports/nsh-look-2026-11/README.md). Audit item 7
(light-theme selection contrast) landed 2026-11-15.
