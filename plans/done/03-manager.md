# 3. Manager: done

**Declared done by the user on 2026-11-11.** M1–M5 are built ([docs/manager.md](../../docs/manager.md)) and a real
TUI GPT-Live call with delegated tools worked on 2026-11-04. The verification items below did not run. They are no
longer the Manager's exit gate. They stay as an unscheduled checklist so nothing is lost. Pick one up only when the
user asks, or when a bug points at it. New Manager work lives in its own sections:
[27 Manager inspector](../27-manager-inspector.md) and [28 Manager indicator and voice](../28-manager-indicator.md).

## Unscheduled verification (was the exit gate)

  running, read a result, approve a waiting action, steer a child, navigate, dismiss a card) with a scratch home and
  scripted provider, in `interface/scenarios/` so both clients share it. The injection and mishearing suite stays.
- [ ] **M0 spikes still unrun:** (a) `pipeline_latency.py` STT → Luna → TTS timings (target under 1.5 s end of speech to
  first audio); (b) `luna_tools.py` tool-choice accuracy; (c) stage mock-ups, look agreed.
- [ ] **Evals with real Luna** (approval needed): retrieval over ~50 sessions and ~100 phrasings (including mangled STT),
  authority, an episode seam, honesty, hand-judged speech quality.
- [ ] **Real voice, each client:** pipeline and GPT-Live with a real microphone and speaker in the TUI and NSH; the
  recent audio-quality polish still needs a listening check.
- [ ] **A 30-minute real call** per client surviving interruption, reconnect and a long delegation (M5 done-when).
- [~] **iOS and native NSH mic/speaker loop** (needed by section 5): Apple native WAV microphone bridge now builds for macOS and iOS; five offline native tests pass. Permission, real recording/playback, and GPT-Live transport remain unverified.
- [ ] **Hands-free (M6):** VAD, barge-in, TUI echo cancellation, notices, optional API-key route for GPT-Live; a week of
  daily use before considering `enabled = true`.
- [ ] **Known boundaries:** authority means "the user spoke" (`docs/manager.md` section 9) and needs a decision; check a
  Manager-started session's worktree after its first turn; Q4 (`navigate` and Settings).
- [ ] **M2 manifest check:** `manager.start` (`/manager`, palette, `alt+m` / `cmd+shift+space`, Q3 to confirm), `manager.talk`,
  `manager.stop` and the engaged keys (design 10.6); run `check_manifest.py`.
- [ ] **Mermaid (step 0):** a real kitty/sixel/iTerm2 terminal and a narrow width; screenshots of the seeded
  `Mermaid diagrams` session in both clients, dark and light; rerun `scenarios.test.tsx` and `mermaid.test.ts` after the lazy
  `nativeModule("nexus/native")` change and the TUI's `cargo test` after the direct-raster change; keep the Ondine pin
  (`b19aa90`) deliberate (an update reruns tests and screenshots; cold CI builds need network for the git dependency).
- [ ] Docs and ledger kept current; tick M0–M6 in `docs/manager-design.md`.
