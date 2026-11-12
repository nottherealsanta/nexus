# 28. Manager indicator and voice control

User request (2026-11-11), four parts:

1. The Manager's dot-wave (braille) animation runs **only while voice is on**.
2. It is **not darkened** when the Manager modal/panel is open.
3. Its **colour follows who is talking** (the user or the Manager).
4. Replace the voice on/off toggle with **Start voice / Stop voice**.

## Today

- TUI `nexus-tui/src/indicator.rs`: states Idle, Thinking, Listening, Speaking… with different speeds and
  brightness; listening uses `manager.listening`, speaking `manager.speaking`, the rest gray; idle is drawn with
  `Modifier::DIM` (~364–369). Waves follow the measured voice level. NSH: `nexus-nsh/src/ui/manager/braille.ts`.
- Decision 2026-11-05: "an open Manager panel dims the rest", which also dims the indicator.
- NSH `ui/manager/stage.tsx` ~121: a `manager.audio_toggle` button labelled `Voice: on` / `Voice: off`
  (manifest title "Toggle Manager audio").
- Manager start/stop commands already exist (`manager.start`, `manager.stop`, `manager.talk`).

## Plan

- [ ] **Clarify one thing with the user:** does "voice on" mean a live call (microphone open, `manager.start`), or
  the audio output toggle (`manager.audio_toggle`)? Default: a live voice call. With voice off, the indicator is a
  static, quiet mark (no motion), still clickable to open the dropdown.
- [ ] **Animation only with voice:** with no live voice, no waves and no animation frames requested (also saves
  redraws). Keep Thinking visible some other way? Default: no; a typed exchange shows its state in the panel.
- [ ] **No dimming:** the indicator is drawn above the scrim when the Manager panel is open, in both clients
  (TUI `paint::scrim` exclusion for the header cell range; NSH z-order). This amends the 11-05 decision; record it
  in [decisions.md](decisions.md).
- [ ] **Colour by speaker:** user speaking → `manager.listening`; Manager speaking → `manager.speaking`; neither
  (listening for speech, silence) → a neutral tone at low amplitude. Check both colours in dark and light themes in
  `interface/theme.toml`, and that they are distinct for colour-blind users (add a shape/speed difference too).
- [x] **Connect / Disconnect and Mute / Unmute (user, 2026-11-12, replaces "Start voice / Stop voice"):** two buttons, each
  labelled by its action, in the dropdown (TUI and NSH) and the phone Manager tab; new `manager.mute_toggle` command and
  `manager.voice` term; the phone tab shows the same braille wave in its title row. Tests: TUI `voice_has_connect_and_mute_buttons`,
  NSH `manager.test.tsx` mute case. Not verified: real microphone mute against the service, phone screenshots, TUI hardware.
- [ ] Tests: indicator unit tests (no frames when voice is off; colour per speaker; not dimmed under the panel),
  NSH equivalents, manifest check; screenshots of the four states in both clients.
- [ ] Docs: `docs/manager.md` "Manager in the clients", `interface/design-language.md`, ledger if old wording is
  referenced.
