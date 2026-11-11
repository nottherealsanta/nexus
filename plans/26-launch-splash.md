# 26. Launch: animate from the first frame, no progress bar

User report (2026-11-11): when `nexus` is opened and the daemon is starting, the opening animation should already be
playing. Remove the progress bar: it never seems to move; it only ever shows `0%` and `daemon`.

## Today

- TUI `nexus-tui/src/splash.rs`: logo + ambient field + a 30-segment **milestone bar** with a percentage and the
  next step name; milestones `["daemon", "workspace", "providers", "session", "ready"]`. "Time never moves the bar."
  It is painted in `main.rs` (~577) before capability queries; ticks every 16 ms (`splash_tick`).
- NSH `nexus-nsh/src/ui/splash.tsx`: the same five steps, bar and `NN%` (`SPLASH_STEPS`, `splashReach`).
- While Core boots, the TUI connection state is `Starting` ("Starting Nexus Core / This takes a moment the first
  time", `render.rs` ~4521). The first milestone is reached only once Core answers, so during a cold daemon start
  the bar sits at 0% for the whole wait. That is what the user sees.
- The `scripts/nexus` launcher runs `cargo run` first, so a rebuild also delays the first frame (not this item's
  concern, but note it when timing).

## Plan

- [ ] **Find why the animation looks stopped during daemon start.** Check `splash_animating` in `main.rs` ~661: it
  should be true from the first frame while Core is `Starting`, and the bootstrap (`discovery.rs`, `nexus daemon
  start`) must not block the event loop. Reproduce with a scratch `$NEXUS10_HOME` and a cold daemon.
- [ ] **Remove the milestone bar, percentage and step label** in both clients. Keep the logo, the ambient field and
  intro particles, and the flash + `▓▒░` wipe into the first live frame on `ready`. Keep milestone tracking
  internally only to know when `ready` is reached.
- [ ] If Core takes long (say > 5 s), show one quiet line under the logo ("Starting Nexus Core…"), and on failure the
  existing error with the daemon log tail (`discovery.rs` ~118). No numbers.
- [ ] Reduced motion / animations off: static logo, straight cut (unchanged).
- [ ] Update the splash module docs, `interface/design-language.md` if it describes the bar, both READMEs; frame
  tests in TUI (`splash.rs` tests assert progress) and the NSH splash test.
- [ ] Verify: cold start with a stopped daemon in Ghostty and in NSH web; record a short capture.
