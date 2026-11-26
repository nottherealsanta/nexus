# 54 — Standalone audio recorder with near-real-time transcription

Status: **planned, not implemented**. Written 2026-11-26. This change adds only this plan.

## 1. User request and scope

User requirements (2026-11-26):

- A standalone recording feature, **not a Manager feature**. No Manager commands, messages, tools,
  audio, consent, or lifecycle are involved.
- An entry point in the top bar, **left of the notifications count/section, which is left of Manager**.
- Open a modal to start recording. The modal shows live transcription and can be minimized back to
  that top-bar section without stopping capture.
- Save an actual audio file (WAV or MP3), rather than only retaining dictated words.
- Aim for **near-real-time transcription**, not updates only every 15–30 seconds.
- Design for both a ten-minute recording and a one-hour recording.

Proposed implementation choices below are engineering defaults, not additional user decisions.
V1 records the microphone only, saves lossless WAV and a transcript, and runs on the TUI and NSH
desktop/web together.
No system/meeting audio, speaker diarization, automatic summary, automatic session insertion, pause,
cloud STT, or Manager integration. MP3 is a later optional export, not required to produce a usable
recording. Mobile is excluded under the existing cut-down-client policy; adding it needs approval.

## 2. Current implementation: reuse and limits

Read these files again before implementation; this is a source snapshot, not a claim about later code.

| Evidence | Current behavior | Consequence |
| --- | --- | --- |
| `docs/voice.md`, `nexus-core/nexus/voice/model.py`, `engine.py` | Local `moondream/parakeet-redux` through the isolated Photon worker; `_PhotonRuntime.transcribe` invokes `transcribe(audio=wav, timestamps="none")` and extracts text | Reuse the pinned model/runtime. The present adapter exposes neither streaming decoder state nor word timestamps. Do not assume upstream streaming support. |
| `nexus-core/nexus/voice/audio.py` | WAV input is mono, 16 kHz PCM16, 0.2–120 seconds | This is a **per-request** limit, not a one-hour session limit. Keep inference windows bounded; do not raise the existing validator to one hour. |
| `nexus-core/nexus/voice/manager.py` | Inference is serialized; busy partial previews fail fast with `voice_busy`; regular work has a pending limit | New recorder scheduling must cooperate with existing voice callers, not create unbounded competing requests. This module manages the STT service, not the product's Manager UI. |
| `nexus-nsh/src/dictation-logic.ts`, `dictation.ts` | Preview eligibility every 0.4 seconds, a 20-second preview window, one preview in flight, and a 120-second capture cap | Live text is possible through repeated short-clip inference, but current cadence is not a measured latency guarantee. Do not extend the dictation buffer indefinitely. |
| `nexus-tui/src/dictation.rs` | Bounded in-memory dictation capture, 120-second maximum, editable composer preview | Extract reusable audio plumbing, but keep the standalone recorder lifecycle and storage separate. |
| `nexus-core/nexus/voice/benchmark.py` | Opt-in cached-model benchmark reports load time, repeated inference timings and RSS | Extend for windowed sustained-load measurements; its existing `real_time_factor` is audio duration / inference time (higher is faster), not inference time / audio duration. |
| `nexus-nsh/src/ui/header.tsx` | Notification count precedes Manager; count is conditional on unread items | Recorder must remain discoverable when there are zero notifications or Manager is disabled. |

The current code does **not** demonstrate reliable hour-long transcription. A single hour-long request
will not work through the current API. Long capture plus short-window STT is the proposed approach.
Consult `docs/voice.md`, `docs/api.md`, `docs/daemon.md`, `docs/stream-events.md`, `docs/tools.md`, and
`.agents/skills/nexus-ui-parity/SKILL.md` before implementation. Native microphone support must be
verified in the actual runtime: existing documentation differs between general dictation and Manager
bridges. A stop-and-return-WAV bridge is not sufficient evidence of continuous chunk support.

## 3. Shared product behavior

### Entry point and modal

Top-bar order: **Recorder → Notifications → Manager**, retaining adjacent existing controls. Idle
label: `Record`. Clicking opens the modal; it does not silently request microphone access or start.
Opening Recorder must work with Manager disabled and must never start it.

Modal contents:

- Title `Recorder`, selected microphone/default-device description, and `Start recording`.
- Separate capture, durability, and transcription statuses; elapsed **captured audio** duration.
- A compact microphone level, scrollable transcript with stable segments and a distinct provisional tail.
- During capture: `Minimize` and `Stop`. No destructive discard alongside Stop.
- After capture: `Save audio…`, `Copy transcript`, `Save transcript…`, `Retry transcription` when needed,
  and access to recent/recovered recordings. Saving uses desktop Save As or browser download as appropriate.
- Transcript rows show recording-relative segment ranges, not invented word timestamps or speaker IDs.

Minimized example: `[● REC 12:34] [Notifications 3] [Manager]`. Clicking the recorder reopens the same
modal. A recording/error indicator persists even when notifications are absent. Narrow desktop
layouts shorten the label before hiding essential state; they must not hide active capture.

Esc, the modal close affordance, and Minimize all minimize while recording. They never discard or
stop audio. Before recording and after completion, Esc closes the modal. Restore keyboard focus to
the entry point; opening the modal traps focus and reopening preserves transcript scroll position.
Typing/Enter elsewhere, switching sessions/workspaces, opening notifications, and model errors do
not stop capture. Do not inherit dictation's stop-on-typing or send semantics.

Stop releases the microphone immediately and drains the last audio chunk independently of STT.
Show `Audio saved · Finishing transcript` if appropriate; downloading/playback of saved audio must
not wait for transcript catch-up. A finished recording can have an incomplete transcript.

Use auto-follow only while the user is at the transcript bottom; otherwise show `Jump to live`.
Do not flood screen readers with every provisional replacement. Match wording, controls, states,
focus behavior and visual meaning in TUI and NSH; shared commands belong in `interface/manifest.toml`.
Do not invent a default shortcut that conflicts with existing bindings. Follow reduced-motion settings.

### Ownership and states

One active recorder per Core initially, with one explicit capture-owner client/lease. Other clients
can view status and saved recordings; a second Start fails clearly rather than stealing the microphone.
Only the owner can submit audio or stop an active capture. A lost owner becomes interrupted after a
bounded heartbeat timeout; lease expiry must also stop capture in the owner client. Reopening from
another client does not imply that its microphone is being recorded. Revisit multi-recorder support later.

Keep independent state axes, not one combinatorial phase enum:

- Capture: `idle → starting → recording → stopping → stopped`, or `interrupted` / `failed`.
- Persistence: durable acknowledged sample offset, local pending samples, and any storage fault.
- Transcription: `preparing`, `live`, `catching_up`, `failed`, `complete`, or `unavailable`.
- View: modal open or minimized; never authoritative for capture.

A generation/lease token invalidates late callbacks after Stop, interruption, retry, or a new recording.
Repeated Stop and finish requests are idempotent. No recorder action changes a session's busy state.

## 4. Architecture: capture first, transcription second

```text
Client microphone → continuous resample → bounded PCM frames → durable chunk transport → Core storage
                                                          ↘ acknowledged audio → STT scheduler
Core storage → recoverable audio chunks → streaming WAV export       ↓
Core metadata + revisioned transcript segments ← final / provisional inference
                                              ↓
                                  modal / minimized status
```

Audio callbacks perform minimal copying and never await disk, network or model work. Move encoding,
resampling and transport off the UI/audio callback path where the runtime supports it. Maintain
resampler state across blocks: rounding each block independently causes drift over an hour. Duration
and transcript positions derive from integer sample offsets, not wall-clock timers.

Reuse capture primitives where sound, but create separate recorder modules in both clients. Do not
remove dictation's limits or change `Ctrl+X V` behavior. Validate a native NSH chunk bridge early; if
one is missing, implement it before claiming desktop-native support. Any platform asymmetry requires
explicit user approval, not a silently disabled shipped control.

### Durable audio and recovery

Proposed internal format: independently checksummed, approximately one-second PCM chunks at 16 kHz
mono PCM16, with monotonically increasing sequence numbers and contiguous sample offsets. WAV is
the first export format. This is speech-quality audio, not an archival stereo/music recorder.
At this format, one hour is 115,200,000 PCM bytes (about 115 MB / 110 MiB), plus metadata.

- Core stores under its configured state home, e.g. `recordings/<opaque-id>/`, not in the workspace,
  model cache, session log or a hard-coded real home. Tests use scratch `$NEXUS10_HOME`.
- Commit chunk files with checksum verification, atomic rename and file/directory durability barriers,
  then advance the metadata checkpoint. Acknowledge only committed data. Define ordering so a crash
  cannot leave metadata claiming audio that is missing; reconcile orphaned committed chunks on restart.
- Client keeps a bounded pending spool until acknowledgement (native protected local spool; browser
  IndexedDB where supported). Detect unavailable/quota-limited browser storage before claiming recovery.
  Never fall back silently to a one-hour RAM buffer. Local and Core copies are distinct durability levels.
- Retry uploads by recording ID + sequence + content hash. Identical retries are idempotent; conflicting
  duplicates or noncontiguous offsets are rejected. Reconnect queries the highest contiguous durable
  offset. After lease expiry, permit the authenticated original owner to seal its locally persisted
  final sample offset and recover only pre-existing spooled chunks up to that offset through a
  recovery-only upload capability. This never renews capture permission or starts a microphone;
  validation and generation checks prevent a different client from appending unrelated input.
- Initial durability target: acknowledge one-second chunks within two seconds p95 on a healthy local
  Core. Display saved-through position and pending duration accurately; do not imply zero-loss capture.
- Initial pending-spool ceiling: 30 seconds. On full disk, unrecoverable write failure, or exhausted
  spool, stop capture visibly, preserve committed audio, and report the exact saved/unsaved range.
  A short network outage may spool locally, but never label unacknowledged bytes `Saved to Core`.
- On crash/reload/lease loss, expose the recording as interrupted, validate checksums and reconstruct
  the contiguous saved range. Do not auto-start the microphone. Resume transcription, not invisible
  capture. Display any holes/truncated tail; never fabricate silence to hide lost audio.
- Assemble WAV by streaming chunks into an export/final file with a valid header and atomic completion;
  no concatenation of WAV headers and no full-hour allocation in browser/Core memory. Keep recoverable
  originals until export is verified. An export error must not destroy source chunks.
- Keep saved recordings across restart. Provide recent recordings in the same modal and explicit
  deletion with confirmation; no automatic expiry in V1. Warn about disk use before starting when
  known free space is inadequate. Deletion cancels jobs and removes server chunks, derivatives and
  client spools; downloaded copies remain outside Nexus's control.

Sleep, tab suspension, device removal and browser close can interrupt physical capture. Detect sample
arrival gaps and show interruption; no promise of recording through sleep or a closed browser. A
navigation/unload warning is best effort, not a durability mechanism. TUI exit should offer Stop and
exit / Keep recording by cancelling exit. Starting on another device is a new recording, not gapless recovery.

## 5. Near-real-time STT strategy

### Initial algorithm (tune through measurement)

Use **bounded repeated inference**, not a claim that Photon is already a streaming decoder.

1. Prepare the cached model on Start in parallel with capture. Missing runtime/model/download consent
   never blocks saving audio: show `Recording audio · Transcription unavailable/preparing`. Download
   only with the existing explicit consent flow. No implicit paid or network fallback.
2. Every roughly **0.75–1 second of new speech**, make the newest eligible active-utterance window
   available for a preview. Permit one recorder inference in flight and at most one replaceable pending
   preview. Stale previews are dropped, never queued in full.
3. Transcribe the growing current utterance, initially bounded to **10 seconds** plus at most one
   second of leading overlap/context. Each preview covers the full active segment, not a sliding
   tail that would make earlier provisional words disappear. Replace only that segment's provisional
   text. Earlier finalized text does not get retranscribed for every update, and the whole recording
   is never resent.
4. Detect pauses initially with lightweight energy-based VAD, tuned against quiet speech, background
   noise and varying devices. Aim to close after **500–800 ms** of silence. Silence remains in saved
   audio even when inference is skipped. Do not treat VAD as proof that no speech exists.
5. On pause, enqueue a durable finalization job for the closed segment. On uninterrupted speech,
   force a boundary at **10 seconds**, retaining **0.5–1 second** of overlap/context.
   Finalization can use slightly wider context than previews; each model request stays below 120 seconds.
6. Stop closes the residual segment, including short tails. The existing STT minimum is 0.2 seconds:
   retain shorter audio, and skip STT or pad only the inference copy, never the saved recording.

Numbers are starting parameters, not measured model capabilities. Measure preview accuracy for the
10-second window versus shorter/longer windows before fixing defaults. Keep forced-boundary and
preview-window bounds consistent when tuning them. End-to-end latency includes chunk formation,
upload and durable acknowledgement before scheduling, not only model inference; the initial
one-second chunks and durability target must be tuned if they consume the preview latency budget.

### Stable text, overlap and timestamps

Store segment ID, sample start/end, revision, text, provisional/final/error status and attempt metadata.
Every result is tagged with its input sample boundary and generation. Ignore obsolete responses.
A provisional tail may revise visibly; a segment becomes final only after its final pass. Explicit
retranscription creates a new revision rather than silently overwriting text already exported/copied.

The current adapter provides text only. Use segment-range timestamps derived from samples; do not
claim exact word alignment. For overlapping segments, test conservative normalized token alignment
at adjacent boundaries. Never remove repeated speech simply because it resembles the prior phrase.
If alignment is ambiguous, preserve the original segment text and mark the uncertain join rather than
silently dropping words. Store raw segment outputs for retry/debugging, outside ordinary diagnostic
logs. Word timestamps or a streaming-capable adapter are optional future work after API verification.

### Backpressure and shared worker

- One centralized scheduling policy must cover the recorder and existing dictation/voice callers;
  bypassing the shared worker lock or spawning a worker per preview is prohibited.
- Prefer durable finalization and interactive work fairly, coalesce previews, and reserve opportunities
  for fresh live previews so a growing finalization backlog does not freeze all visible text.
- Pending finalization is a disk-backed set of segment records, not unbounded WAVs/tasks in RAM.
  Read bounded windows on demand. Retry failed segments with bounded backoff; keep explicit failure
  state after the retry limit so Stop cannot wait forever. User can retry later from saved audio.
- Under load, lower preview frequency first and show `Catching up · 18s behind`. Compute lag from
  processed audio offsets, not request counts; report preview freshness separately from finalization lag.
- Report saved-audio and transcript status independently. A busy/missing/crashed worker never stops
  otherwise healthy capture. Do not cancel unrelated inference when dismissing the recorder modal.

## 6. Performance targets and feasibility gate

Targets for the chosen reference machine with a warm model and healthy local Core:

| Metric | Initial acceptance target |
| --- | --- |
| End of a spoken word → provisional text containing it | p50 ≤1.5 s, p95 ≤3 s |
| End of utterance → finalized segment visible | p95 ≤4 s, including pause detection |
| Preview cadence while speaking | Normally about 1 s; explicitly degraded if hardware cannot sustain it |
| Sustained capture | Ten minutes and sixty minutes with no unreported audio gaps |
| Long-run STT throughput | No steadily growing finalization backlog at the selected live-preview cadence |
| Memory | Bounded live buffers/queues; no growth proportional to audio duration or number of UI updates |

These are **goals, not current guarantees**. Cold model load is measured separately; recording still
starts. Transcript text/metadata may grow on disk, but the UI paginates/virtualizes history and retains
only a bounded active viewport/tail. Define and record per-process memory budgets after baseline
measurement; a simple one-shot speed ratio is insufficient to pass the gate because previews repeat work.

Before building the entire UI, run a cached-model feasibility spike on the user's target hardware.
Measure cold/warm load, end-to-end preview and finalization latency distributions, CPU/GPU/RSS,
accuracy on short windows, boundary word errors, and a sustained simulated live workload that includes
both previews and finals. Benchmark representative consented speech, including continuous speech,
silence, repeated phrases, names/numbers and background noise. Use human/reference-aligned word
positions for latency measurement; never derive word timing from segment boundaries alone.

If targets fail: tune cadence/window/VAD/device selection and compare quality; then report measured
limits. A different model/runtime or true streaming adapter needs an explicit follow-up decision,
license/runtime review and benchmark. Do not silently replace the configured model or describe a
15–30-second refresh as near-real-time. Audio-only recording remains useful on slower machines.

## 7. Core API and event contract (proposed)

Add a dedicated recorder service/control API, not an agent task tool and not a Manager endpoint.
Names below are proposed; follow current daemon routing/storage conventions when implementing.

- `POST /api/recordings`: create capture with idempotency key, input format and owner identity; return
  opaque ID, lease token and initial state. Starting requires an explicit user action.
- `PUT /api/recordings/{id}/chunks/{sequence}`: bounded PCM body, sample offsets, checksum and lease;
  returns durable contiguous checkpoint. Validate exact PCM size/alignment, hash and limits.
- `POST /api/recordings/{id}/heartbeat`: renew capture lease; use existing client identity infrastructure.
- `POST /api/recordings/{id}/stop`: idempotently declare final sample count after the last upload.
  Distinguish confirmed complete capture from interruption with unreceived audio.
- `GET /api/recordings` and `GET /api/recordings/{id}`: bounded recent list and authoritative state;
  paginated segment reads and durable checkpoint permit reconnection without replaying all text.
- `GET /api/recordings/{id}/audio.wav`: streamed saved audio with interruption/incompleteness status
  available before download. Exporting active capture is either a clearly labelled snapshot or rejected.
- Transcript export (TXT and structured JSON with segment times/status) and retry selected failed/all
  segments after capture; retries are revisioned and do not duplicate persisted segments.
- `DELETE /api/recordings/{id}`: explicit confirmed deletion; reject active capture until stopped.

Publish bounded state/checkpoint and revisioned segment events through the existing event channel.
Coalesce/drop live provisional updates safely; authoritative snapshots recover state after reconnect.
Keep final segments durable and queryable. Do not stream PCM into the global event log or overload
session wire events. Event payloads include recording ID, generation and monotonic revision.

Reuse daemon authentication/origin protections and filesystem access policy. Never expose arbitrary
filesystem paths in API requests; export via existing user-controlled Save As/download mechanisms.
Apply body limits, filename sanitization, owner-token checks, restrictive storage permissions and
bounded list/pagination limits. Do not put audio, transcripts or lease tokens in routine logs/metrics.
Audio is personal data: no automatic agent/model prompt insertion and no hidden upload beyond the
configured Core. If Core is remote, clearly state audio is saved there; show the destination before Start.
No change to session context/timeline semantics: only an explicit later user action can copy/insert text.

## 8. Implementation sequence and completion criteria

All boxes are open. No implementation or performance claim is implied by merging this plan.

- [ ] **A — Feasibility and runtime audit.** Re-read current voice/capture code; benchmark windowed STT
  and identify reference hardware; validate native continuous capture. Document measured latency,
  memory and quality here. Resolve platform blockers before claiming desktop parity.
- [ ] **B — Durable recorder service.** Implement metadata, chunk commit/ack protocol, ownership/leases,
  auth, bounded transport and recovery, streaming WAV export, recent recordings and deletion.
  Fake audio can complete a one-hour recording without loading any STT runtime.
- [ ] **C — Windowed STT scheduler.** Add VAD/segmentation, coalesced previews, durable final jobs,
  revisioned results, conservative overlap handling and fair sharing of the existing worker.
  Prove stale-result rejection, bounded memory, and audio independence with fake inference.
- [ ] **D — Both desktop clients together.** Implement separate recorder capture controllers, pending
  spools, shared modal/header semantics and commands in TUI + NSH. Include failure/degraded states,
  save/copy/retry and accessibility. Keep ordinary dictation behavior unchanged.
- [ ] **E — End-to-end hardening.** Execute ten-minute and sixty-minute real capture runs with consent;
  worker crash, Core restart, reconnect, disk-full, device removal and client-close recovery. Compare
  audio sample counts/checksums and transcript coverage against the source. Record actual latency
  percentiles, gaps, lag, memory and limitations; don't pass on a fast synthetic-only test.
- [ ] **F — Documentation and paired verification.** Update `docs/voice.md`, API/event docs, both client
  READMEs, and a porting-ledger row if behavior differs from old Nexus. Add this plan to `plans/README.md`
  and record landed implementation in `plans/landed.md` when it actually lands. Keep this planning-only
  change to the single requested file. Follow the screenshot skills and shared scenario conventions.

Likely code boundaries: new recorder package/service and daemon routes near existing voice routes;
reusable audio helpers under `nexus-core/nexus/voice/`; dedicated recorder controllers/UI in
`nexus-nsh/src/` and `nexus-tui/src/`; shared command definitions in `interface/manifest.toml`.
Determine exact routing/persistence integration paths during A rather than inventing existing modules.

## 9. Test matrix and verification

### Deterministic tests (no microphone/model/network required)

- Continuous resampling across odd block sizes and sample rates; sample counts and WAV header integrity;
  valid empty/short recordings and an hour of generated audio streamed without full allocation.
- Chunk retries, conflicting duplicates, reordered/missing data, bad hashes, oversized bodies, unauthorized
  owners, lease expiry, repeated Stop, stop/upload races, orphan chunks and interrupted export recovery.
- Crash injection before/after each durability boundary; recovered files match acknowledged offsets.
- VAD with quiet speech/silence/noise; forced boundaries with repeated words/numbers; ambiguous overlap
  does not silently delete content; final tiny tail; stale out-of-order inference cannot change new state.
- Slow/busy/dead inference, missing runtime/model, failed download, bounded retry and recovery; capture
  and export succeed independently. Fairness tests with existing dictation/voice consumers.
- Bounded transport spool and UI history; disconnect/reconnect and disk/quota errors; no false `Saved`
  indicator. Persisted transcript is reconstructible from snapshot + revisions after event loss.
- No automatic session message, prompt insertion, Manager start/route call or microphone access merely
  from opening a modal. Recorder remains functional with Manager disabled and zero notifications.

### Shared TUI/NSH scenarios and visual checks

Cover idle modal → explicit Start → provisional revisions → finalized segments → minimize → navigate
sessions → reopen → Stop → audio saved/transcript finishing → completed export. Also cover audio-only,
catching up, interrupted capture, recovery, deletion, keyboard focus/Esc, long transcripts and narrow
screens. Capture and inspect matching screenshots for active modal, minimized recording, completion
and storage/STT errors using `nexus-tui-screenshots` and `nexus-nsh-web-screenshots`.

Run focused Core pytest and Ruff checks, relevant TUI tests then `cargo test`, relevant NSH web tests
via `node scripts/nsh-web.mjs test --target web <file>.test.tsx`, and manifest/theme validators when
changed. Run real microphone/model tests only with user consent; no implicit downloads, paid calls,
real-home migration or destructive changes. Report unavailable platforms and unrun checks explicitly.

### Real-hardware acceptance record (fill during implementation)

Record hardware/OS, client/runtime, model/device, cold/warm behavior, capture duration, source and
saved sample counts, acknowledged-loss bound, preview/final latency p50/p95, final backlog, process
RSS over time, boundary accuracy, and outcomes of fault/recovery scenarios. Record native/web
suspension limitations separately. Release as near-real-time only for configurations that pass the
measured gate; label other configurations as degraded, not as lossless or gapless by assumption.

## 10. Deferred choices

- MP3 export with an explicit encoder/dependency decision; WAV alone satisfies V1 audio persistence.
- Higher-quality/stereo originals, system audio, pause/resume, speaker labels and transcript editing.
- A true streaming model/adapter if measurements show repeated inference cannot meet the target.
- Multiple simultaneous recorders and active capture handoff between clients.
- Mobile, background mobile recording and OS-level global recording shortcuts (separate approval/scope).
