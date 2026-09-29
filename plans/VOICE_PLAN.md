# Voice input plan (Parakeet Redux)

Add local, offline speech-to-text dictation to Nexus. The user presses a key or
the mic control, speaks, and the transcript lands in the composer, ready to edit
and send. The same feature works in `nexus chat` (Textual) and `nexus web`
(browser). The model is **Moondream Parakeet Redux**
(`moondream/parakeet-redux`, a 1.58-bit build of `parakeet-tdt-0.6b-v3`,
178 MB of weights, multilingual, running on Photon from `moondream>=2.4.0`). It
is **downloaded and loaded automatically the first time Nexus starts** and is
kept warm afterwards.

This plan follows the rules in `AGENTS.md`: one-way layering, UI through the host
contract, line budgets, a durable log first, bounded everything, and loopback
only.

---

## 1. Goals and non-goals

**Goals**

1. Dictate into the composer in both surfaces, with the same key, the same
   control, the same wording and the same states.
2. Fully local: audio never leaves the machine and no network access is needed
   after the one-time download.
3. Zero setup: on the first launch of Nexus the model downloads in the
   background, shows progress, and loads. Later launches warm it from the cache.
4. Chat is never blocked or slowed. If voice is unavailable (offline, missing
   microphone, unsupported platform, download failed), chat works as before
   and the mic control explains why.
5. Fast: on Apple silicon (`mps`) or 8 CPU cores, a 10-second clip should
   transcribe in well under a second once the model is loaded.

**Non-goals (this plan)**

- Text-to-speech or spoken replies.
- Live word-by-word streaming captions while speaking. Phase 5 adds
  near-live chunked partials as an optional follow-up.
- Voice commands that act without review. A transcript is always a draft. It is
  never sent automatically unless the user turns on an explicit setting.
- Storing audio. Recordings are transient.

---

## 2. Key decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Where inference runs | **In the daemon**, in a new `nexus/voice/` manager | One copy of the 178 MB model per workspace daemon, shared by the TUI and browser. Clients stay thin, per rule 4. |
| Where the microphone is captured | **In the client** (browser `getUserMedia`, TUI `sounddevice`) | The daemon is a detached background process. macOS attributes microphone permission to the terminal or browser the user is looking at, and the browser can only capture from the page anyway. |
| Audio on the wire | 16 kHz mono 16-bit PCM WAV, resampled by the client | This is what the model wants. It is small (32 KB/s), and one format keeps validation simple. |
| Transport | TUI: `VoiceTranscribe` host command over UDS (bytes field, 16 MB frame cap). Browser: dedicated `POST /v1/web/voice` raw-body route with its own cap | The shared `/v1/web/command` body cap is 1 MiB. A separate route raises the cap for audio only, instead of raising it for every command. |
| Model storage | Global, `~/.nexus/models/voice/parakeet-redux/<revision>/` | Download once per machine, not per workspace. Pinned revision and hashes. |
| Durable log | **No change to the session log or reducer** | A transcript is composer draft text, like typed text. Only the sent message becomes durable, through the normal `SessionStart`/`SessionEnqueue` path. |
| Recording interaction | **Toggle** (press to start, press again to stop and transcribe; Esc cancels) | Terminals do not deliver key-up events, so push-to-talk cannot work in the TUI. The web uses the same toggle for parity. |
| Dependency | `moondream>=2.4.0` as a normal dependency, imported lazily. `sounddevice` for TUI capture | Autoload on first open needs the runtime already installed. Lazy import keeps `nexus run` and tests from paying for it. Phase 0 confirms wheel size and platforms. If the wheel is too heavy, fall back to a `voice` extra that `nexus doctor` points to. |

---

## 3. Architecture

```
TUI (ui_support/voice_capture.py: sounddevice → 16k PCM WAV)
   └─ VoiceTranscribe(audio=bytes) ──UDS──┐
Browser (js/voice.js: getUserMedia → AudioWorklet → 16k WAV)
   └─ POST /v1/web/voice (raw WAV) ─HTTP─┤
                                         ▼
                        host/facade.py  HostFacade.handle
                                         │  (validate, bound, redact)
                                         ▼
                        nexus/voice/manager.py  VoiceManager
                           ├─ store.py    download, pin, verify, lock, cache path
                           ├─ engine.py   Photon wrapper (lazy import, 1 worker thread)
                           └─ audio.py    WAV parse/validate, duration and size bounds
                                         │
                        moondream.photon("moondream/parakeet-redux", device=…)
```

Daemon start → `Runtime` builds `VoiceManager` → if `voice.enabled` and
`voice.autoload`, the daemon schedules `VoiceManager.prepare()` as a background
task: download if missing, then load. The status is exposed through
`VoiceStatus` and included in `Doctor`.

### Layer placement

- `nexus/voice/` is a **manager (L3)** package. It may import only L0
  (`config/`, `errors.py`, `util.py`) and the standard library, plus lazy
  `moondream`. It never imports `core/`, `host/` or `ui/`. Add it to the
  layering checks in `tests/test_layering.py` (a new parametrized case like the
  model-layer one).
- `runtime.py` (composition root) constructs it and owns its lifecycle.
- `host/protocol.py` and `host/facade.py` expose it. `host/web.py` adds one
  route.
- Clients: `nexus/ui_support/voice_capture.py` (TUI audio capture, no Textual
  imports), `nexus/ui_support/tui_voice.py` (the Textual mic chip and recording
  indicator), and `nexus/ui/web/js/voice.js`.

### Line budgets (measured now)

| Area | Now | Cap | Headroom | Voice adds (target) |
| --- | --- | --- | --- | --- |
| `host/` | 6,853 | 7,000 | 147 | ≤ 80: protocol structs about 35, facade about 20, web route about 25 |
| `ui/` | 4,856 | 5,000 | 144 | ≤ 40: `app.py` wiring and the `SHORTCUTS` row, CLI `/voice` spec |
| `core/`+`model/` | — | 14,000 | — | 0 |

All real logic goes in `nexus/voice/` (not budgeted) and `nexus/ui_support/`
(not in the `ui/` budget). JS and CSS are not counted. This split is where the
code belongs anyway, so it doesn't move code only to dodge a budget. If `host/`
still gets too tight, WAV validation and status projection go to
`nexus/host_support/voice.py`.

---

## 4. Model lifecycle: "auto-load on first open"

### 4.1 States

`VoiceState` is a frozen struct in `nexus/voice/model.py`:

```
disabled | unsupported | absent | downloading | loading | ready | error
```

It also has `progress` (0 to 1 while downloading), `bytes_done`/`bytes_total`,
`device` (`mps`/`cuda`/`cpu`), `revision`, `message` (redacted and short), and
`since`.

### 4.2 First launch flow

1. Any surface opens Nexus, so the daemon auto-starts (`host/daemon.py:start`).
2. `Runtime` builds `VoiceManager(config.voice, home=~/.nexus)`. The build is
   cheap and does no I/O.
3. After the daemon is serving, it calls `runtime.voice.schedule_prepare()`,
   which runs as a background task that is never awaited on the request path:
   - Take an inter-process file lock at `~/.nexus/models/voice/.lock`
     (`fcntl.flock`), so two workspace daemons never download at once. The
     second daemon waits for the lock, then finds the files present.
   - If the files are missing for the pinned revision, the state becomes
     `downloading`. Download into `…/<revision>.partial/`, check each file's
     size and SHA-256 against the pinned manifest, then `os.replace` it into
     place. Cap the total download at 256 MB. Time out stalled reads after
     60 s. Retry with backoff, up to 3 attempts.
   - The state becomes `loading`. Import `moondream`, then open
     `md.photon(<local path or repo id>, device=<resolved>)` on the voice
     worker thread.
   - The state becomes `ready`. Run one silent 0.5 s warm-up clip so the first
     real dictation doesn't pay kernel JIT or Metal compile time.
4. While `downloading` or `loading`, the daemon's idle-shutdown timer treats the
   task as activity (the same way a running turn does), so a first launch
   followed by closing the window doesn't leave a half-finished download.
5. A failure leaves the state at `error`, with a redacted message such as "Voice
   model download failed (offline?). Chat is unaffected." The next daemon
   start, `/voice download`, or the Settings "Retry" button tries again.

### 4.3 Later launches

The files are present, so preparation skips straight to `loading` → `ready` in
the background, usually in a second or two. Loading is lazy if
`voice.autoload = false`: the first press of the mic loads the model and shows
"Loading voice model…".

### 4.4 Memory

The model stays resident while the daemon lives. The daemon already exits when
idle. An optional `voice.unload_after_minutes` (default 0, meaning never)
releases it when a machine is short on memory.

### 4.5 Phase 0 spikes: Photon behavior to confirm before building

These change details of `store.py` and `engine.py`, not the design:

- **Download location and control.** Does `md.photon("moondream/parakeet-redux")`
  download itself (Hugging Face cache)? Can it take a local directory, or does
  it honor `HF_HOME` or a `cache_dir` argument? Preferred order: (a) we download
  with our own bounded, pinned, hash-checked downloader and pass a local path;
  (b) we set a private cache dir and pin the `revision`; (c) we let Photon
  download, then verify.
- **Progress callback** for downloads. If there is none, measure the size of
  the `.partial` directory.
- **Input types.** `transcribe(audio=…)` is documented with a path. Check whether
  it accepts bytes, a file object, or a NumPy array. If it only takes a path,
  write a `0600` temp file under `<workspace>/.nexus/cache/voice/`, then delete
  it in `finally`.
- **Thread safety and reentrancy.** Assume it is not thread-safe. Use one worker
  thread and serialize calls.
- **Device auto-selection** (`None` → CUDA → MPS → CPU) and its cost on Intel
  Macs without AVX-512 VNNI. Measure the real-time factor. If it is below 5×,
  default `device` to `cpu`, or warn in `nexus doctor`.
- **Wheel size, supported platforms, and Python 3.11/3.12/3.13 wheels.** Also
  whether any telemetry or API key is involved in local Photon use. It must not
  be. If it is, disable it and state that in `README.md`.
- **Weights license.** Record it in `nexus/voice/NOTICE` and `README.md`.

---

## 5. Configuration

A new `VoiceSection` goes in `nexus/config/schema.py` (frozen, `forbid_unknown_fields`),
wired into `ConfigV2` and the merge order in `config/layers.py`:

```toml
[voice]
enabled = true            # master switch; false hides the mic control
autoload = true           # download + load on daemon start
model = "moondream/parakeet-redux"
revision = "<pinned sha>" # pinned; bumped deliberately with new hashes
device = "auto"           # auto | cpu | mps | cuda
max_seconds = 120         # hard cap per recording (client + server enforce)
timestamps = false        # reserved for later (segment/word)
auto_send = false         # insert only; never send by default
unload_after_minutes = 0
```

The environment override `NEXUS_VOICE=off` disables it everywhere. CI and the
test suite use this, see §10.

---

## 6. Host contract

### 6.1 Protocol (`nexus/host/protocol.py`)

Add these to the command and result unions (additive, so `PROTOCOL_VERSION`
stays 3 unless the review decides otherwise):

| Command | Fields | Result |
| --- | --- | --- |
| `VoiceStatus` | — | `VoiceStatusResult(state, progress, bytes_done, bytes_total, device, revision, message, max_seconds, enabled)` |
| `VoicePrepare` | `force: bool = False` | `VoiceStatusResult` (starts or retries download and load, returns immediately) |
| `VoiceTranscribe` | `audio: bytes` (WAV), `session: str = ""` (only for log correlation), `request_id: str` | `VoiceTranscribeResult(request_id, text, duration_s, elapsed_s, language: str = "")` |
| `VoiceCancel` | `request_id: str` | `VoiceCancelResult(cancelled: bool)` |
| `VoiceRemove` | — | `VoiceStatusResult` (deletes cached weights; used by Settings) |

`msgspec` JSON encodes `bytes` as base64. A 120 s clip is about 3.8 MB of WAV,
or about 5.1 MB of base64, which fits well inside the 16 MB UDS frame.

### 6.2 Facade (`nexus/host/facade.py`)

Each handler is a thin call into `runtime.voice`:

- Validate before doing anything: the WAV header (RIFF, PCM format 1, 1
  channel, 16 kHz, 16-bit), `len(audio) ≤ 44 + 16000*2*max_seconds`, and a
  duration of at least 0.2 s. Reject with a typed `ErrorResult` code:
  `voice_unavailable`, `voice_not_ready`, `voice_busy`, `voice_too_long`,
  `voice_bad_audio`.
- If the model is not ready, return `voice_not_ready` with the current state
  instead of blocking. Clients show "Loading voice model…" and retry once it is
  `ready`. On a `VoiceTranscribe` while `absent`, the facade also kicks
  `prepare()`.
- Concurrency: one transcription at a time per daemon. A bounded queue of 2
  returns `voice_busy` beyond that. Each request has a timeout of
  `max(10 s, 2 × duration)`.
- Logs record only the duration, elapsed time, device and outcome. **Never the
  transcript text or audio.**
- `Doctor` gets a `voice` block (state, device, revision, cache path, real-time
  factor of the warm-up) for `nexus doctor` and the details panel.

### 6.3 Browser route (`nexus/host/web.py`)

`POST /v1/web/voice?request_id=…` takes a raw `audio/wav` body.

- The same auth as `/v1/web/command`: cookie session, exact `Origin`, and
  `X-CSRF-Token`.
- A per-route body cap of 8 MiB. The global `DEFAULT_MAX_BODY_BYTES` (1 MiB)
  stays unchanged for every other route. This needs a small hook in
  `http_sse.py` that lets a route declare its own larger limit before the body
  is read.
- Dispatches `VoiceTranscribe` to the facade and returns the result JSON.
- Add `Permissions-Policy: microphone=(self)` to the page response. The CSP
  needs no change: the AudioWorklet module loads from `'self'` under
  `script-src 'self'`, and `connect-src 'self'` covers the POST.
  `http://127.0.0.1` counts as a secure context, so `getUserMedia` works.

`VoiceStatus`, `VoicePrepare`, `VoiceCancel` and `VoiceRemove` go through the
normal `/v1/web/command`.

### 6.4 Client (`nexus/client/protocol.py`)

Add `voice_status()`, `voice_prepare()`, `voice_transcribe(wav, request_id)`,
`voice_cancel(request_id)` and `voice_remove()` for the TUI and CLI.

---

## 7. The `nexus/voice/` package

| File | Contents |
| --- | --- |
| `__init__.py` | Contract docstring (local-only, bounded, transient audio); exports `VoiceManager`, `VoiceState`. |
| `model.py` | `VoiceState`, `TranscribeResult` frozen structs; error codes. |
| `audio.py` | `parse_wav(data) -> PcmInfo`, strict validation, `silence(seconds)` for warm-up. Pure; no third-party imports. |
| `store.py` | Paths (`models_root(home)`), pinned manifest (`files: {name: (size, sha256)}`), `ensure(progress_cb)` with lock, `.partial` dir, hash check, atomic rename, `remove()`. |
| `engine.py` | `PhotonEngine`: lazy `import moondream`, device resolution, single `ThreadPoolExecutor(max_workers=1)`, `load()`, `transcribe(wav_bytes) -> text`, `close()`. An `Engine` protocol so tests inject `FakeEngine`. |
| `manager.py` | `VoiceManager`: state machine, `schedule_prepare()`, `status()`, `transcribe()`, `cancel()`, `remove()`, `shutdown()`; bounded queue; timeouts; idle-unload; activity flag read by the daemon's idle timer. |
| `NOTICE` | Model and license attribution (packaged via `pyproject.toml` package-data). |

`Runtime.__init__` accepts `voice_engine_factory=None` (defaulting to
`PhotonEngine`), the same way it accepts `providers=`. `Runtime.close()` calls
`voice.shutdown()`.

---

## 8. Surfaces (kept in parity)

Both surfaces get the same control, key, command, states and wording.

### 8.1 Shared behavior

| Element | Behavior |
| --- | --- |
| Mic control | A chip at the end of the composer's second row, after effort: `mic`. It is dim when the model isn't ready and accent-colored while recording. |
| Shortcut | **Ctrl+Space** toggles recording (added to `ui/tui/app.py:SHORTCUTS` as "Dictate (voice input)" so `/hotkeys` and the web list show it). **Esc** cancels a recording without transcribing. Phase 1 confirms that the terminal delivers Ctrl+Space through `ui/tui/keys.py`, since some terminals send it as NUL (`ctrl+@`). If it doesn't arrive reliably, fall back to **Ctrl+Y** in both surfaces. |
| Chat command | `/voice` in `ui/cli/commands.py:SPECS`: `/voice` toggles recording, `/voice status`, `/voice download` (runs `VoicePrepare`), `/voice off\|on` (sets `voice.enabled` via the Settings write path). Mirrored in web `SLASH_COMMANDS`. |
| Activity bar while recording | `● rec 0:07` with a small level meter (5 cells). At `max_seconds − 10` it switches to warning color, and at `max_seconds` recording stops and transcribes automatically. |
| Transcribing | The activity bar shows motion plus "transcribing…". The composer stays editable. |
| Result | The text is inserted **at the cursor**, with a space before it if needed. The composer is focused. Nothing is sent unless `auto_send = true`. An empty result shows a toast: "Didn't catch that". |
| Model not ready | Pressing mic shows "Voice model downloading 42% (178 MB)" or "Loading voice model…" in the activity bar. It records anyway once the state is `loading`, and transcription waits and retries at `ready`. While `downloading` it doesn't record. |
| Top bar status on first launch | While downloading, the status area appends `· voice 42%`, then clears at `ready`. It is quiet otherwise. |
| Errors | One red line in the activity bar with a redacted reason (no mic permission, no input device, voice unavailable). Chat is unaffected. |
| Settings → Voice | A new group in both Settings pages: enabled, auto-send, device, max length, status line (state, device, revision, cache size), and **Download / Retry / Remove model** buttons. |
| Details sidebar | A `Voice` row in `SESSION` or a small `VOICE` block showing state and device, from `Doctor`. |

### 8.2 TUI (`nexus chat`)

- `nexus/ui_support/voice_capture.py` (no Textual): `Recorder` uses
  `sounddevice.InputStream(samplerate=16000, channels=1, dtype='int16')` with a
  bounded ring buffer (`max_seconds`), RMS level callbacks, and `stop() -> wav
  bytes`. `sounddevice` is imported lazily. If it is missing, or PortAudio has no
  input device, the error is `voice_no_input`.
- `nexus/ui_support/tui_voice.py` (Textual allowed; add it to the list in
  `tests/test_ui_layering.py` and `AGENTS.md` rule 2): `VoiceChip` widget,
  `VoiceIndicator` for the activity bar, and a `VoiceController` that runs
  record → `client.voice_transcribe` → `ChatEditor.insert_at_cursor`. It follows
  the race rule by re-checking `controller.session` and the focus target before
  inserting.
- `ui/tui/app.py`: about 15 lines to mount the chip in `RootAgentBar`, add the
  `SHORTCUTS` row and the binding, and route `/voice` in
  `_dispatch_chat_command`. All behavior lives in `tui_voice.py`.
- Status polling reuses the existing panel polling (`panels.py`), so there is
  no new timer loop. Poll `VoiceStatus` every 1 s only while the state is
  `downloading` or `loading`.
- Add `sounddevice` to `ALLOWED_THIRD_PARTY` for `ui_support/voice_capture.py`
  only.

### 8.3 Web (`nexus web`)

- `nexus/ui/web/js/voice.js` (new ES module): `startRecording()` gets
  `getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}})`,
  then an `AudioWorkletNode` (`js/voice-worklet.js`) downsamples to 16 kHz Int16
  into a bounded buffer and reports RMS. `stopRecording()` builds the WAV and
  calls `api.voice(wav, requestId)`. `cancelRecording()` is also exported. The
  module releases tracks on stop so the browser's mic indicator turns off.
- `api.js`: `voice(blob, requestId)` sends a POST to `/v1/web/voice` with the CSRF
  header.
- `index.html`: a `#composer-voice` chip in `.composer-context` after
  `#reasoning-effort` (stable ID for Playwright), plus a hidden
  `#voice-indicator` inside `#activity-bar`. No inline scripts or styles.
- `app.js`: a keydown handler for Ctrl+Space and Esc, a `/voice` entry in
  `SLASH_COMMANDS`, a Settings → Voice group, and a top bar status suffix. Stale
  results are dropped with `state.voice.requestId`.
- `app.css`: styles for the recording and level meter in Signal style (square,
  no blur, accent while recording). Respect reduced motion by showing a static
  dot instead of the pulsing meter.
- Also listed in `docs/web.md`'s parity table.
- `pyproject.toml` package-data already covers `js/*.js`.

### 8.4 CLI

- `nexus voice status|download|remove` for scripting and troubleshooting.
- `nexus voice transcribe FILE.wav` prints the text, for manual verification and
  benchmarks.
- `nexus doctor` prints the voice block.
- `nexus run` and JSONL: no change.

---

## 9. Security and privacy

- Loopback only. No new listener. The browser route has the same
  cookie/Origin/CSRF checks.
- Audio exists only in memory, or in a `0600` temp file that is deleted in
  `finally` if Photon needs a path. It is never written to session logs, daemon
  logs, `LogsRead` or exports.
- The transcript is logged nowhere by the daemon. It becomes durable only if
  the user sends it as a message.
- Download: HTTPS only, pinned revision, per-file size and SHA-256 manifest
  checked before activation, a 256 MB total cap, and an atomic rename. A
  hash mismatch deletes `.partial` and sets `error`. The manifest is updated
  deliberately in a PR, never at runtime.
- The browser microphone is gated by the browser's own prompt plus
  `Permissions-Policy: microphone=(self)`. The TUI microphone is gated by the
  macOS microphone prompt for the terminal app. `nexus doctor` explains how to
  grant it.
- Everything is bounded: recording length, body size, queue depth, request
  timeout, download size, and retries.

---

## 10. Testing

The whole suite stays offline and needs no model. `tests/conftest.py` gets an
autouse fixture that sets `NEXUS_VOICE=off` unless a test opts in, and voice
tests inject `FakeEngine` (returns fixed text, can block or fail on demand).

| Test file | Covers |
| --- | --- |
| `tests/test_voice_audio.py` | WAV parsing: good file, wrong rate/channels/format, truncated, oversized, too short. |
| `tests/test_voice_store.py` | Pinned-manifest download against a local fake server or a monkeypatched fetch: progress, hash mismatch → error and cleanup, `.partial` never activated, lock prevents a double download (two processes), `remove()`. |
| `tests/test_voice_manager.py` | State machine (`absent → downloading → loading → ready`, `error` then retry), autoload scheduled on start without blocking, busy queue, timeout, cancel, idle unload, `NEXUS_VOICE=off` → `disabled`, activity flag. |
| `tests/test_host_voice.py` | Facade commands, error codes, bounds, and that transcript and audio never appear in daemon logs or `LogsRead`. `Doctor` includes voice. |
| `tests/test_web_transport.py` (extend) | `/v1/web/voice`: auth, CSRF, Origin, 8 MiB cap (1 MiB elsewhere unchanged), `Permissions-Policy` header, CSP unchanged. |
| `tests/test_layering.py` (extend) | `nexus/voice` imports only L0 and stdlib (plus lazy `moondream`). |
| `tests/test_ui_layering.py` (extend) | `sounddevice` allowed only in `ui_support/voice_capture.py`; `moondream` and `sounddevice` are not imported by `nexus run`, CLI or TUI startup. |
| `tests/test_tui_voice.py` | Pilot: Ctrl+Space toggles with a fake recorder, indicator states, insertion at cursor, Esc cancels, not-ready messaging, the `/voice` command, the Settings Voice group. Add a `PanelTransport` fake for `VoiceStatus`. |
| `tests/test_tui_spacing.py` / `test_tui_layout.py` | The mic chip doesn't disturb composer spacing. |
| `tests/playwright_web_check.py` (extend) | Chromium with `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream` and a scripted daemon plus `FakeEngine`: click `#composer-voice`, wait, stop, check the text is inserted, check Esc cancels, check the not-ready state, screenshots at 1440/1024/400 in dark and light. |
| `tests/test_voice_live.py` (`@pytest.mark.live`) | Real Photon plus model: transcribe `tests/fixtures/voice/hello.wav` (a short, freely licensed clip), check key words and real-time factor. Deselected by default. |

---

## 11. Delivery phases

Each phase ends green: `pytest -q`, `ruff check nexus tests`, and the budget
tests.

**Phase 0: spike (about 1 day)**
Answer §4.5 in a scratch script, record the findings at the top of this file,
and pin the revision and manifest. Measure load time and real-time factor on
`mps` and `cpu` on this Mac. Decide between a normal dependency and a `voice`
extra.
*Exit:* a written spike result and a pinned manifest.

**Phase 1: engine and daemon (backend only)**
`nexus/voice/` package, `VoiceSection` config, `Runtime` wiring, background
autoload on daemon start, idle-timer integration, protocol commands, facade,
`Doctor` block, client methods, `nexus voice …` CLI, and tests
`test_voice_*`, `test_host_voice.py` and the layering tests.
*Exit:* on a clean machine (`rm -rf ~/.nexus/models/voice`), `nexus doctor`
shows voice `downloading` then `ready`, and `nexus voice transcribe x.wav`
prints the text.

**Phase 2: TUI dictation**
`voice_capture.py`, `tui_voice.py`, `app.py` wiring, `SHORTCUTS` row, `/voice`,
top bar download status, Settings → Voice, and pilot tests.
*Exit:* in `nexus chat`, Ctrl+Space → speak → Ctrl+Space inserts the text, and a
first launch shows the download progress.

**Phase 3: web dictation (parity)**
`/v1/web/voice` route with a per-route cap, `Permissions-Policy`, `voice.js`,
worklet, chip, indicator, keys, `/voice`, Settings → Voice, details row, and the
Playwright check with a fake microphone.
*Exit:* the same flow in `nexus web`, screenshots match the TUI placement, and
the transport tests pass.

**Phase 4: docs and polish**
Update `README.md` (voice usage, privacy, model and license, how to disable),
`docs/core.md` (voice manager and commands in the host table),
`docs/textual.md` and `docs/web.md` (file maps and parity rows), `AGENTS.md`
rule 2 (the new `tui_voice.py` Textual file), and `EXTENDING.md` (how to add
another speech engine through the `Engine` protocol).

**Phase 5: optional follow-ups**
- Near-live partials: the client sends 5 s rolling chunks and the daemon
  transcribes incrementally with segment timestamps, updating a ghost preview in
  the composer.
- Voice activity detection to auto-stop after about 1.5 s of silence.
- A `transcribe` tool so agents can transcribe audio files in the workspace,
  under `tools/permissions.py` path checks.
- Parakeet Ultra (full precision) as an alternative `voice.model` on CUDA.

---

## 12. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Photon downloads on its own and ignores our cache or pins | Phase 0 decides. Prefer our own downloader plus a local path. Otherwise pin `revision` and verify hashes after download. |
| `moondream` wheel is heavy or missing on some platforms | Lazy import and the `unsupported` state. Fall back to an optional `voice` extra, with `nexus doctor` guidance. |
| Ctrl+Space not delivered by some terminals | Detect in Phase 2. Ctrl+Y fallback. `/voice` and the mic chip always work. |
| First-launch download on a metered or offline network | Background, non-blocking, a 178 MB size shown in the status, `voice.autoload = false` or `NEXUS_VOICE=off` to opt out, retry on demand. |
| Memory pressure from a resident model in several workspace daemons | `unload_after_minutes`. The model is small (178 MB). The daemon's idle exit already releases it. |
| `host/` or `ui/` line caps | Logic lives in `nexus/voice/` and `ui_support/`. The targets are about 80 and 40 lines, well inside the 147 and 144 headroom. |
| Background-noise accuracy (9.04 WER vs 6.72 for the original) | Browser `noiseSuppression`. The transcript is always an editable draft. Document Parakeet Ultra for GPU users. |

---

## 13. Files touched (summary)

New:
`nexus/voice/{__init__,model,audio,store,engine,manager}.py`, `nexus/voice/NOTICE`,
`nexus/ui_support/voice_capture.py`, `nexus/ui_support/tui_voice.py`,
`nexus/ui/web/js/voice.js`, `nexus/ui/web/js/voice-worklet.js`,
`tests/test_voice_{audio,store,manager,live}.py`, `tests/test_host_voice.py`,
`tests/test_tui_voice.py`, `tests/fixtures/voice/hello.wav`.

Changed:
`pyproject.toml` (deps and package-data), `nexus/config/schema.py`,
`nexus/config/layers.py`, `nexus/runtime.py`, `nexus/host/protocol.py`,
`nexus/host/facade.py`, `nexus/host/daemon.py` (schedule prepare, idle
activity), `nexus/host/web.py`, `nexus/host/transports/http_sse.py` (per-route
body cap), `nexus/client/protocol.py`, `nexus/cli.py`, `nexus/ui/cli/commands.py`,
`nexus/ui/tui/app.py`, `nexus/ui/tui/app.tcss`, `nexus/ui/tui/panels.py`,
`nexus/ui_support/tui_widgets.py` (chip slot in `RootAgentBar`, insert at
cursor), `nexus/ui_support/tui_settings.py`, `nexus/ui/web/index.html`,
`nexus/ui/web/js/{app,api}.js`, `nexus/ui/web/styles/app.css`,
`tests/conftest.py`, `tests/test_layering.py`, `tests/test_ui_layering.py`,
`tests/test_web_transport.py`, `tests/playwright_web_check.py`, and the docs
listed in Phase 4.
