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

User decision (2026-11-12): the "voice on" concept is gone. The Manager voice state is only Connected / Disconnected
and Muted / Unmuted (the two buttons below). Recorded in [decisions.md](decisions.md).

- [x] **Decision recorded (replaces "Clarify with the user"):** no "voice on/off" wording, setting, command or state
  remains in Core, TUI, NSH or docs (grep clean; `manager.audio_toggle` is the Connect / Disconnect command).
- [x] **Animation only while connected:** TUI `IndicatorState::Still` (disconnected) and `Muted`, NSH `WaveMode`
  `still` / `muted`: no drift, no animation frames requested. Muted treatment (picked): a frozen flat line at 0.55
  brightness; disconnected: a frozen quiet wave at 0.40 brightness (the dimmest). Both gray; ASCII glyphs `·` / `–`
  (`-`), caption word "Muted" where the field cannot say it. A typed exchange shows its state in the panel.
- [x] **No dimming:** TUI restores the strip's cells after `scrim_by`; NSH cuts the backdrop around the header
  indicator's measured box (full-window backdrop is the fallback). Amends 11-05 ([decisions.md](decisions.md)).
- [x] **Colour by speaker:** user speaking -> `manager.listening`; Manager speaking -> `manager.speaking`; neither
  (idle, thinking, still, muted) -> `text.muted`. Unit tests in both clients. Speed and amplitude differ too (speaking
  fastest, listening level-driven).
- [x] **Connect / Disconnect and Mute / Unmute (user, 2026-11-12, replaces "Start voice / Stop voice"):** two buttons, each
  labelled by its action, in the dropdown (TUI and NSH) and the phone Manager tab; new `manager.mute_toggle` command and
  `manager.voice` term; the phone tab shows the same braille wave in its title row. Tests: TUI `voice_has_connect_and_mute_buttons`,
  NSH `manager.test.tsx` mute case. Not verified: real microphone mute against the service, phone screenshots, TUI hardware.
- [x] Tests: TUI `indicator_is_still_unless_the_voice_is_connected`, indicator unit tests (still/muted never drift,
  colour per speaker); NSH `manager.test.tsx` "indicator states", `manager-cards.test.tsx` scrim cut-out.
- [ ] Screenshots of the four states (connected speaking/listening, muted, disconnected) in both clients, and a
  check that NSH's measured indicator box is window-relative on every target (only the cut-out's presence is tested).
- [x] Docs: `docs/manager.md`, both READMEs, `interface/manifest.toml`, `interface/design-language.md`, decisions.
