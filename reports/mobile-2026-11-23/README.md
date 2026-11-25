# Phone conversation navigation and composer (2026-11-23)

Dark and light 390×844 captures of the phone client with the tab bar hidden in a conversation, an icon-only back
chevron, the live `Working · Ns` header, the compact two-row composer, prose in Google Sans with Monospace code, the
drawer by its left-edge swipe, and the root tabs and surfaces after the change.

Run from the repo root after `cd nexus-nsh && NEXUS_PHONE_PREVIEW=1 npm run build` (then `npm run gen` restores the
preview flag):

```sh
node reports/mobile-2026-11-23/shots.mjs nexus-nsh/dist/Nexus 4899 reports/mobile-2026-11-23
```

The API is mocked; no Core or provider is used. `9-conversation` is the waiting state with the approval card,
`10-idle-composer` is an empty conversation, `11-busy-header` shows `Working · Ns` with long agent/model names,
`12-composer-focused` is the focused input with a draft, `13-user-details` is the long-press reveal of the message
time, number, copy and fold controls, `14-back-sessions` is the returned Sessions tab, and `15-drawer` is the
edge-swipe drawer. These are web captures at a phone viewport, not a device or simulator: they do not show a software
keyboard or real home-indicator insets.
