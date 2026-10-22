# Local dictation (`nexus/voice/`)

Optional, consent-gated speech-to-text for the composer. Audio is transient and
bounded; the TUI and web use the host contract and never touch the runtime or the
model cache. Plans and spike notes: `plans/VOICE_PLAN.md`, `plans/VOICE_SPIKE.md`.

Voice status reports `cached` separately from the in-memory lifecycle state.
Native `/voice` and Ctrl+X, V automatically load cached files (including after
idle unload), without an extra confirmation, then start recording with the
existing start sound once ready. Loading/downloading shows progress or
a refresh action instead of another download prompt. Cache-only preparation
uses `VoicePrepare.allow_download=false`, so missing or invalid files cannot
silently trigger a download. A missing cache still requires explicit consent.
Dialog polling never retries a failed load automatically; errors remain visible
alongside the retry action, not only in inspect details. Scheduled preparation
publishes loading immediately, before the background task runs, so clients do
not mistake its initial response for a failed load.
Voice settings retain a separate preparation action and
never start capture.
Cached weights do not include the Python voice runtime. Both download-enabled
and cache-only preparation check runtime availability first and report the
`nexus voice init` instruction instead of a generic initialization error.

## Files

| File | Owns |
| --- | --- |
| `voice/manager.py` | `VoiceManager`: serialized lifecycle and inference; ≤ 1 active + 2 waiting requests; `Engine` and `Store` protocols |
| `voice/store.py` | pinned manifest (six files with sizes and SHA-256): `model.bark` from `ConwayResearch/Underdog-Bark-3.0-Clean` at `6d34ed86…` and five NVIDIA processor files (`config.json`, `generation_config.json`, `processor_config.json`, `tokenizer.json`, `tokenizer_config.json`) from `nvidia/parakeet-tdt-0.6b-v3` at `541d1f99…`; download to `~/.nexus/models/voice/bark-3.0-clean/<revision>`, ≤ 256 MiB, interprocess lock, atomic install, only verified files activate; the old `parakeet-redux` cache is removed |
| `voice/bark.py` | strict reader for the `bark-levels-v2/v3` container (header, tensor sizes, element budget and LZMA output are validated before use; decoded one tensor at a time) |
| `voice/engine.py` | `BarkEngine`: decodes `model.bark` into a stock Transformers `ParakeetForTDT`; `AutoProcessor` and `GenerationConfig` load from the verified directory with `local_files_only=True`. No `trust_remote_code`, no downloaded Python, no pickle/`torch.load`, no NVIDIA weights |
| `voice/audio.py` | strict WAV validation: mono, 16 kHz, PCM16, ≥ 0.2 s, ≤ `voice.max_seconds` (≤ 120) |
| `voice/model.py` | `VoiceState`, `TranscribeResult`, `VoiceError` |
| `host_support/voice.py` | bounded, redacted host dispatch and Doctor projection |
| `ui_support/voice_capture.py`, `ui/ratatui/voice.py`; `ui/web/js/voice.js`, `voice-worklet.js` | TUI and browser capture and consent flow |
| `ui/ratatui/voice.py`, `ui/web/js/voice-strip.js` | the live dictation strip (waveform + running transcript) |

## Flow

1. **Install:** `install.sh` adds the `voice` extra (`transformers`, `torch`,
   `constriction`, `numpy`) by default, except on musl systems such as Alpine, Intel
   Macs (no Torch wheels) or with `--no-voice`; `sounddevice` is always installed.
   Torch's current wheels cover macOS arm64, Linux x86_64/aarch64 (glibc) and
   Windows x86_64 on Python 3.13/3.14; the supported set is Torch's, and
   `constriction` wheel coverage and a full resolve are **not verified**. Any other
   install gets both pieces from `nexus voice init`: when Torch, Transformers or
   `constriction` is not importable it adds
   the runtime (a uv tool is reinstalled at the same version and source with
   `voice` added to its extras, so `nexus update` keeps it; `nexus update` also
   keeps `voice` whenever the runtime is importable or a model is downloaded, so a
   receipt that lost the extra (installs from before 0.3.3, a plain reinstall) does
   not drop dictation, and `--no-voice` opts out; editable and pip
   installs get the extra's requirements, read from package metadata, through
   `uv pip install --python <interpreter>` or `pip`), restarts the workspace
   daemon and downloads the model in a fresh interpreter, since a uv reinstall
   replaces the running venv. It refuses on musl. With the runtime present it is
   `nexus voice download`. The model (~180 MB) is **not** bundled.
2. **Consent:** the first use shows a confirmation dialog in the TUI and web;
   `nexus voice download` is itself an explicit action. `VoicePrepare` carries no
   consent token, so UI consent is a client-flow contract, not host-enforced.
3. **Prepare:** the daemon may warm a *verified* cache at startup but always passes
   `allow_download=False`; ordinary startup and transcription never download.
   An absent model stays cached-only.
4. **Capture:** clients send bounded mono 16 kHz PCM16 WAV (`VoiceTranscribe`; the
   browser uses `POST /v1/web/voice`, 8 MiB cap). The transcript is inserted as
   editable composer text; `auto_send` is off by default. In the native Ratatui client,
   Enter during recording stops capture and sends the completed message after
   final transcription, even with `auto_send` off. Escape and other stopping keys
   keep the final transcript without sending, even with `auto_send` on. Automatic
   capture-limit stops retain the `auto_send` preference. Explicit discard remains
   a separate cancellation path. Failed or empty transcription does not send.
5. **Live preview:** while recording, clients re-send the growing recording as
   `VoiceTranscribe(partial=True)` (web: `?partial=1`), request id
   `<recording id>-p<n>`. At most one preview is in flight, sent only after
   ≥ 0.4 s of new audio and no sooner than `max(0.7 s, 1.5 × last preview
   time)`, so slow devices self-throttle. The manager answers a partial with
   `voice_busy` at once when any inference is running or queued, so previews never
   delay the final transcript; clients ignore preview failures. On stop the
   in-flight preview is aborted and `VoiceCancel`led, and the final transcript of
   the whole recording (not the last preview) is what reaches the composer.
   Engine work is not interruptible, so a final may still wait for one running
   preview. Re-transcribing the whole recording each time is quadratic in its
   length; it stays bounded by `max_seconds` (≤ 120). Latency on real hardware is
   **not verified**.
6. **Cancel/remove:** `VoiceCancel`, `VoiceRemove`, `nexus voice remove`.

Config: `[voice]` (`enabled`, `autoload` false, `max_seconds`, `device`
`auto|cpu|mps|cuda`, `auto_send`, `unload_after_minutes`); `model` and `revision`
are pinned to the one trusted manifest (Bark `6d34ed86…`). `NEXUS_VOICE=off` disables it.

## Decisions and caveats

- Dictation uses Underdog Bark 3.0 Clean, a Parakeet TDT 0.6B v3 fine-tune that also
  drops fillers and false starts, through stock Transformers. It replaces the Kestrel
  runtime (`moondream/parakeet-redux`); `voice.model` and `voice.revision` in an old
  config are now rejected, so delete them.
- Weights are decoded to float32 on the worker thread, one tensor at a time, then
  moved to the device: expect roughly 2.4 GB for the model plus the 180 MB file
  while loading. CPU inference is capped at 4 threads. These figures are estimates;
  memory, speed and the platforms above are **not measured**.
- Real inference and network behavior are **not verified**. Do not describe the path
  as proven offline.
- The weights and processor files are CC-BY-4.0; keep the attribution in
  `nexus/voice/NOTICE`.
- Legacy/web UI behavior (orange dot only while recording, the floating live strip with
  waveform and preview text, any key stops, `Esc` discards, no auto
  "loading/ready" model labels): [surfaces.md](surfaces.md#dictation).

## Adding or replacing an engine

Implement the `Engine` protocol (`load`, `transcribe(bytes)`, `close`) in
`nexus/voice/`, keep dependency imports lazy, preserve the bounded WAV,
serialized worker, model-store and host-command boundaries, and cover it with a
fake-engine unit test. No UI may import the inference library or read the cache.
Tests: `tests/test_voice_*.py`, `test_ratatui_voice.py`.

The native voice dialog shows only the actions for its current phase. Runtime
errors and unsupported installations show the host's message and Retry, even
before preparation is requested; a loaded model offers Start dictation and
replaces the download invitation.

## Live transcript previews

The legacy TUI displays a floating waveform and transcript tail above the composer,
without moving the composer. Growing WAV snapshots are bounded by the capture
limit; at most one preview is in flight, with pacing based on inference latency.
`VoiceTranscribe.partial` defaults to false and is carried by the client and web
endpoint. Partial requests bypass final-result deduplication, never mark a
session as voice-used, and do not insert text. Stopping cancels the preview and
requests a final transcript of the full recording; only that result enters the
composer. Legacy/web Escape discards capture and cancels inference; native
Ratatui Escape finishes and keeps the final transcript without sending.

The browser renders the same preview phases with a canvas waveform. Real-
microphone latency and inference on supported hardware are **not verified**.

`nexus voice` defaults to status. Missing-runtime download failures point to
`nexus voice init` once, without appending a second installation recipe.


The native Ratatui client uses the same host-backed TOML voice settings helper
for `/voice on|off`. It observes `enabled`, `max_seconds` and
`auto_send`, and submits only a final transcript combined with the existing
composer draft. A late result is discarded if the session or active panel
changed. Partial transcripts are previews only. Native capture tests use a fake
recorder; physical microphone and model-runtime parity remain unverified.

Native Ctrl+X, V starts capture immediately when voice is enabled and its model is
ready. Setup retains a bounded dialog when enablement or download is needed. Live
preview words resolve changed ASCII letters over three 125 ms frames, preserving
stable preceding words, whitespace and Unicode. Final transcription remains the
only inserted text. The Ctrl+X leader has no visible shortcut banner. Physical
microphone latency and real inference remain unverified.

Native dictation now previews directly inside the editable composer, at the capture
insertion position. Recording moves out of the composer onto the activity rule directly
under the composer card: cell 2 is the red recording square (blinking every 4 spin frames
between error red and a dimmer mixed red; `#` in ASCII), cell 3 is the activity rule, and
cells 4-8 show the elapsed recording time (`mm:ss`) in red; while transcribing, neither mark
is shown on the rule. The floating waveform/status strip is removed. Typing
stops capture and also applies the typed key. Final text replaces the temporary
preview at the captured position, preserving typed suffix text. Escape stops and
keeps the final transcript without sending; Enter stops and sends. Both explicit
choices override `auto_send`. Only a default stop (`send=None`, including the
capture limit) uses that preference; explicit discard cancels without insertion.
The composer grows for live previews. Real microphone/model latency is unverified.

## Start and stop cues

Agent completion in the native client uses the same generated PCM and
`sounddevice` playback path with a distinct rising two-note cue. It does not
use macOS system sounds. `NEXUS_COMPLETION_SOUNDS=off` disables completion cues
independently of recording cues; playback is best-effort without audio support.

Starting dictation plays a short rising two-note cue; stopping plays a falling
one. The cue (`ui_support/voice_capture.py:play_cue`, via `sounddevice`
output) finishes before the microphone opens so it is not recorded. It is
terminal-only (Ratatui); the web app has none. Cues are best-effort (a
missing output device is silent) and `NEXUS_VOICE_SOUNDS=off` disables them. Native
Escape plays the stop cue while keeping the transcript; explicit discard also
plays the stop cue. Audible result on
real hardware is not verified.


## Speak the latest answer (terminal clients)

`/speak` reads only the final, completed assistant message of the most recent
completed session turn. It never reads earlier assistant messages (including
tool-call preambles), tool calls/results, reasoning blocks, or child-agent
transcripts. An active turn, missing final text, or a tool-call-only final
message is refused. Markdown in the final answer is passed through as text.
Playback happens on the daemon machine's default output device, not remotely
on the client. Both Ratatui dispatch the same `Speak` host command;
the deprecated browser client has no new speech UI.

Install in the environment used to run the daemon (source checkout shown):

```sh
uv sync --extra speak
# macOS (Intel or Apple Silicon)
brew install portaudio
# Debian/Ubuntu instead
sudo apt-get install libportaudio2
nexus daemon restart
```

The speak extra is `ptts` (`>=0.4.1,<1`), `sounddevice` and `numpy`. `ptts` is the
official Rust runtime (a Python wheel) for gradium's phonon models and needs only
numpy: no torch, onnxruntime, phonemizer or spaCy.

The model is [`gradium/phonon-31m-en`](https://huggingface.co/gradium/phonon-31m-en)
(Apache 2.0), loaded as `ptts.TTS("hf://gradium/phonon-31m-en", lang="en",
quant="q8")`: the int8 variant of a 31M-parameter English model, run on the CPU
with the model's default voice. ptts itself fetches the files from the Hugging Face
Hub into the standard cache (`models--gradium--phonon-31m-en`), about 60 MB
(`MODEL_BYTES`, approximate and used only for the progress display). The worker
splits the answer into sentences (at most 400 characters each, cut at the last
space that fits), synthesizes one chunk at a time with `tts.synth(text)` and plays
each as float32 audio at the model's sample rate through `sounddevice`. The
reported backend is `ptts-cpu`.

**Download flow (like `/voice download`).** `/speak` first asks the host for the
model status (`SpeechStatus`: `unsupported`, `absent`, `downloading`, `ready`,
`error`; status never imports ptts, it looks for the packages `ptts`, `sounddevice`
and `numpy` and the Hugging Face cache files, and a snapshot with a leftover
`*.incomplete` blob is not ready). When the model is missing both clients show a
consent dialog with the size (about 60 MB) and say it runs on this device. Confirming
sends `SpeechPrepare`, which starts one background download in the isolated worker
(it loads the model online, so ptts fetches it); the dialog polls the status and shows
`Downloading… 42% · 25 / 60 MB` (progress is the Hugging Face cache blobs growing
against the approximate total), then offers "Speak latest answer" (or "Done"
when opened from `/speak download` or Settings → Speech). A failed download shows
the reason and a Retry; missing packages show the install steps below and only a
Close button. `/speak download` with the model present just says it is ready.
Settings → Speech shows the model state with a Download button.

**Upgrades never ask again.** Consent is durable: `SpeechPrepare` writes
`~/.nexus/models/speech/consent`, and an earlier Paradee or Kokoro model already in
the Hugging Face cache counts as earlier consent (both were only fetched after the
dialog). When the model is missing after that (a release that switches models,
an upgrade, a cleared cache), `SpeechStatus` starts the download itself and
reports `downloading`; `/speak` then shows the progress, refreshes it every
second (bounded to 10 minutes) and speaks when it is ready, with no dialog. Leaving
the page stops the wait; the download continues. A failed automatic download
shows the error and Retry and is not retried in a loop. `nexus update` keeps the
`speak` extra (see [release.md](release.md)).

Playing is cached-only: the worker runs with `HF_HUB_OFFLINE=1` and a model that is
not cached is reported as not cached, never fetched. `Speak.download` still exists
on the wire for scripts but the clients use `SpeechPrepare`. Installing dependencies and the initial
download require network access; the answer is synthesized locally, not sent
to a speech service. Hugging Face stores model files in its standard cache.

Synthesis runs on the CPU only, through the ptts runtime. There is no device
setting and no GPU or MPS path, and `NEXUS_SPEAK_DEVICE` is ignored. The `[speech]`
keys (`voice`, `language`, `speed`, `device`) keep their single supported values
(`af_heart`, `a`, `1.0`, `cpu`) for config compatibility; the worker does not pass
them to ptts, which uses the model's default voice. Windows
needs working PortAudio output; not verified.

**Esc.** Speaking shows no notice (only a failure does). Both clients run the
request in the background so keys are still read; Esc sends `SpeakStop`, which
tells the warm worker to stop playing (the model stays loaded). Esc does nothing
extra when nothing is playing.

**Warm worker.** The first `/speak` starts a long-lived worker process that
imports ptts and loads the model. The model then stays in memory, so later answers
start without reloading.
The daemon unloads it (kills the worker, freeing the memory) after 10 idle minutes
(`IDLE_UNLOAD_SECONDS`) and the next `/speak` loads it again. Esc stops the
current answer without unloading. A failed request, a timeout or a download-mode
change also drops the worker.

One speech request runs at a time, with no queue. The worker has a five-minute
wall-clock timeout, a 20,000-character input limit and a three-minute generated
audio limit; exceeding a limit is reported, not silently clipped. Each sentence
plays as soon as it is synthesized, so an answer that crosses the audio limit is
spoken up to that point and then reported. The worker lives between requests (see Warm worker) and exits on idle unload or daemon exit.
Environment changes require restarting the daemon. Actual speaker playback, Windows
support and other CPUs are **not verified**; offline tests use fake `ptts` and
`sounddevice` modules (numpy is real). Latency, speed and memory of the phonon model
on real hardware are **not measured** here; the earlier Paradee benchmark no longer
applies.
