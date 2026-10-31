# Voice

Adapted from old Nexus `docs/voice.md`. Models come from the standard Hugging Face
cache (`$HF_HUB_CACHE`, `$HF_HOME/hub`, `$XDG_CACHE_HOME/huggingface/hub`, then
`~/.cache/huggingface/hub`). Nexus keeps no copy of its own. The runtime is the
optional extra: `nexus voice init` (Moondream Photon through `moondream`, PyTorch,
numpy, Hugging Face Hub and ptts).

Defaults, changeable in `config.toml`: `[voice] stt_model =
"moondream/parakeet-redux"`, `[speech] tts_model = "gradium/phonon-31m-en"`.

The STT model is Moondream's packed ternary Parakeet Redux checkpoint (177.8 MB
of weights, 179 MB including its config, tokenizer and ternary manifest). Photon
reads the base-3 packed checkpoint; the CPU and Metal paths keep the packed
codes, and CUDA uses Kestrel's resident dense form. Nexus does not decode a
custom weight container or build an f32 Transformers model.

## Status

`GET /api/voice` returns `{stt, tts}`, each `{state, repo, revision, cache_path,
size, error}`. `state` is `unsupported`, `runtime_missing`, `absent`,
`downloading`, `ready`, `loaded` or `error`. It never loads a model or downloads
(`nexus/voice/hf_cache.py`, `nexus/voice/status.py`). STT uses one pinned
snapshot: `moondream/parakeet-redux` at revision
`2bf128600aac4b16946f7ed8372e56117fe5e23b`, with `model.safetensors`,
`config.json`, `ternary.json` and `tokenizer.json`. The manifest sizes and
SHA-256 hashes are checked before a download is reported as ready. For an absent
pinned model `size` is the expected download size, so consent dialogs show the
real figure. The installed runtime must be exactly `moondream 2.6.1` with
`kestrel 0.9.1`; any other version is reported as `runtime_missing` with a
reinstall message.

STT inference is supported on macOS 14 or newer on Apple silicon (the locked
PyTorch 2.15 wheel is macOS 14+), and on glibc Linux x86-64 (glibc 2.28 or newer)
and aarch64 (glibc 2.34 or newer). The native Kestrel and PyTorch wheels have
CPython 3.14 builds for those platforms. The model weights are CC-BY-4.0. The
Photon runtime is installed separately from PyPI; at the version examined, its
`kestrel-kernels` dependency is proprietary M87 Labs software and Kestrel
enables basic usage telemetry by default. Nexus does not redistribute the
runtime. Review those terms before enabling the voice extra; see
`nexus-core/nexus/voice/NOTICE`.

## Speech to text

`POST /api/voice/transcribe` takes a raw WAV body (not JSON): mono, 16 kHz,
PCM16, 0.2–120 seconds. It returns `{text}`. The transcript is only a response;
Core never adds it to a prompt or sends it to a model. The client can place it
in the editable composer. `?partial=1` requests a live preview; if any inference
is already running, Core immediately returns 409 `{error, code: "voice_busy"}`
instead of queuing the preview. Invalid WAVs return a 4xx JSON `{error, code}`.

`POST /api/voice/stt/download` explicitly starts the pinned Hugging Face
download and returns 202. This is the only STT download path: a status check or
transcription never downloads missing weights. Progress is published as the
global `voice.progress` event with `model: "stt"`, `state`, `bytes_done`,
`bytes_total` and `error`. Core verifies every manifest size and hash before
reporting `ready`. The worker writes to the standard HF cache, not a Nexus model
directory.

The Photon runtime is not imported or started at daemon startup.
`POST /api/voice/stt/prepare` starts it from the verified local snapshot before
capture; transcription also starts it on demand. STT runs in one isolated child
process (`nexus/voice/worker.py`) so the daemon's environment is never changed:

- The parent passes `MODEL_ID` only to select the registered Photon family; the
  verified local `model_path` is authoritative for the checkpoint, tokenizer and
  ternary manifest. The worker gives Photon an owned directory of symlinks, so
  Kestrel takes its local checkpoint branch. A missing or deleted cache fails as
  a missing file instead of an implicit `snapshot_download`.
- Kestrel's telemetry reporter is replaced by a no-op and its Hugging Face
  config probe is disabled. A process-local Python socket guard, best effort and
  not an OS sandbox, rejects non-loopback DNS lookups and connections made
  through Python's socket module. Native runtime libraries can bypass it; the
  exact reviewed versions and offline mode are the primary guarantees.
- The reviewed runtime is exactly `moondream 2.6.1` with `kestrel 0.9.1`. The
  worker checks the installed distributions and fails closed on any other
  version instead of silently applying the private telemetry/probe shims to an
  unreviewed release.
- The verified sizes and SHA-256 hashes are checked by Core before the worker
  starts. A missing or corrupt snapshot fails closed.

Inference is serialized on the single worker. After 10 idle minutes the worker
is stopped and its memory released; the next request starts a new one from the
cache. Errors include 409 `voice_busy`/`voice_model_not_cached` and 422
`voice_dependency_missing`/`voice_model_invalid`/`voice_load_failed`.

### Migrating from Underdog Bark

Older configuration files may still say
`[voice] stt_model = "ConwayResearch/Underdog-Bark-3.0-Clean"`. That engine is
no longer supported. `GET /api/voice`, `nexus voice status` and dictation report
an explicit message asking you to set `[voice] stt_model =
"moondream/parakeet-redux"` in `config.toml` and download the new model. Nexus
does not rewrite the setting automatically and does not delete the cached Bark
files as part of the migration. An explicit Remove action in Settings still
deletes only the configured repository after its own confirmation.

## Speech

`POST /api/voice/speak {session_id}` reads the final text of the session's latest
completed turn (thinking and tool calls are never spoken) and streams `audio/wav`
(mono PCM16; the header sizes are `0xFFFFFFFF` because the length is not known).
The client plays it. The daemon runs a warm `ptts` worker in a child process,
offline (`HF_HUB_OFFLINE=1`), unloaded after 10 idle minutes. Errors are JSON
`{error, code}`: 409 `speech_busy`/`speech_turn_active`, 404 `speech_no_answer`,
422 for the rest (`speech_model_not_cached`, `speech_dependency_missing`, …).
`POST /api/voice/speak/stop` stops the current answer; the worker stays loaded.

## Notification sounds

Both clients play two generated cues, best effort through the default audio
device: a rising two-note cue when a session finishes, and a higher two-note cue
when a session waits for an approval or an answer. The finished cue follows the
session state, so a subagent finishing sounds when its own turn ends and the
root sounds again only when its whole tree is idle. Replayed events, snapshots
and services without an output device stay silent. `NEXUS_COMPLETION_SOUNDS=off`
(also `0` or `false`) disables both cues. Settings › Notifications › Notification
sounds is the per-client switch; it is on by default. The TUI plays them through
`rodio` in `nexus-tui/src/dictation.rs`; NSH uses Web Audio oscillators in
`nexus-nsh/src/cues.ts`.

## TUI

`/speak` (command `session.speak`, palette) plays the viewed session's latest answer:
`nexus-tui/src/speech.rs` posts to `/api/voice/speak`, reads the WAV header for the
sample rate and appends each PCM chunk to a `rodio` player on its own thread, so
audio starts with the first sentence. `session.speak_stop` (palette) stops the local
player and calls `/api/voice/speak/stop`. Only one answer is spoken at a time. An
ignored test (`cargo test --lib speaks_a_real_answer -- --ignored`, with
`NEXUS_SPEAK_URL`, `_TOKEN`, `_SESSION`) plays through the real device.

### Dictation

`/voice`, `Ctrl+D` or `Ctrl+X V` (`voice.dictate`) checks `GET /api/voice`. If the model is
absent a consent modal (repo, size) comes first, then `POST /api/voice/stt/download` with
progress; `runtime_missing` and `unsupported` show the status error as a toast. The TUI
prepares the model before capture, playing a repeating tone while a cold model loads.
`nexus-tui/src/dictation.rs` then captures with `cpal` on its own thread, downmixes and resamples
to 16 kHz mono PCM16, plays start and stop cues, shows a blinking red square only during capture, and sends
paced `?partial=1` previews (at least 0.4 s of new audio apart) that appear inside the
composer at the cursor where recording began. Enter stops and sends; Esc stops and keeps the
words (Esc while transcribing discards them); typing stops the capture and the key applies.
Cues use the default output device, trying its supported formats if its default format fails;
if the device cannot be opened, a warning appears and dictation still works. Set
`NEXUS_VOICE_SOUNDS=off` to silence TUI cues. To exercise the actual output device, run
`cargo test --lib plays_dictation_cues_on_real_output -- --ignored` in `nexus-tui`.
Real microphone capture is not yet checked.

## NSH (browser and desktop)

`/speak` and the Stop action (`nexus-nsh/src/speech.ts`, `speech-format.ts`) call
`/api/voice/speak` through the same-origin API path. The WAV header gives the sample rate,
PCM16 chunks are reassembled across odd byte boundaries and scheduled with Web Audio as
they arrive, so audio starts with the first sentence. Stop aborts the fetch, stops the
scheduled audio and calls `/api/voice/speak/stop`. One answer plays at a time, and API
errors show as toasts. Real playback against Core is not yet checked in a browser.

### Dictation

A mic button in the composer and `voice.dictate` (`nexus-nsh/src/dictation.ts`,
`dictation-logic.ts`, `voice-state.ts`) capture with `getUserMedia` (AudioWorklet, with a
ScriptProcessor fallback), encode a 16 kHz mono PCM16 WAV in the page and post it through
the same-origin API path. Live previews are paced like the TUI's. Enter or Stop inserts the
final transcript as editable draft text without sending; Esc cancels. A missing model asks
for consent before the download and shows progress in the composer. Before capture, the
client prepares the model, playing a gentle repeating tone while a cold model loads; rising
and falling tones mark capture starting and stopping. A denied microphone is
a toast. Not yet checked in a browser.

## Installing the runtime

The heavy packages (Moondream Photon, PyTorch, numpy, Hugging Face Hub and ptts) are
the optional `voice` extra and are never installed with Nexus. `nexus voice init`
installs them into the environment Nexus runs from (`uv pip install --python <that
python>`, or `pip` without uv), prints the model status and asks you to restart the
daemon (`nexus daemon stop`). `nexus voice status` shows the status without installing.
The extra pins `moondream==2.6.1` and `kestrel==0.9.1`; the worker refuses any other
installed version rather than applying the reviewed private shims to it.
Models are still downloaded only on request. In a development checkout `uv sync`
removes these packages again; use `uv sync --extra voice` there instead.

## Settings: Voice and speech

Both clients show the models in Settings → Voice and speech (manifest section `dictation`):
state, repo, size, cache path and any error (for example `nexus voice init`) per model, from
`GET /api/voice`. Download asks for consent first (repo, about 179 MB for speech to text and
65 MB for text to speech, from Hugging Face into the standard cache), then calls
`POST /api/voice/stt/download` or `/tts/download` and follows `voice.progress`. Remove
confirms (Cancel is the default) and calls `DELETE /api/voice/{stt|tts}`, which deletes only
that repo's folder from the cache; it is refused while a download runs or speech plays. Rows
are disabled with a reason when they make no sense in the current state. The settings are
read on open and after each action. Not yet checked against real downloads or in a browser.

## Benchmark

A real audio/model benchmark is opt-in and never downloads anything. Download the model
through the app first, then from `nexus-core`:

```sh
uv run --extra voice python -m nexus.voice.benchmark speech.wav --device cpu --repeat 3
```

It uses the same isolated offline worker and prints load time, per-run transcription
time, real-time factor and RSS. **No real audio or model benchmark has been run for this
change**; the results above are not measured numbers.

## Verification status

Decision (2026-10-29): section I is accepted without a run against real audio, a real
model download or a browser. Everything above is covered by unit, fake-worker and API
tests. The Bark path is gone; its cached files are left for the user to remove. The
pinned Parakeet Redux hashes and allowlist come from the Hugging Face revision metadata.
Real microphone capture, the real STT download and transcription, and playback and
Settings in a browser are not verified. The opt-in benchmark above has not been run.
