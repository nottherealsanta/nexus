# Local dictation (`nexus/voice/`)

Optional, consent-gated speech-to-text for the composer. Audio is transient and
bounded; the TUI and web use the host contract and never touch the runtime or the
model cache. Plans and spike notes: `plans/VOICE_PLAN.md`, `plans/VOICE_SPIKE.md`.

## Files

| File | Owns |
| --- | --- |
| `voice/manager.py` | `VoiceManager`: serialized lifecycle and inference; ≤ 1 active + 2 waiting requests; `Engine` and `Store` protocols |
| `voice/store.py` | pinned model manifest (revision `2bf12860…`, four files with sizes and SHA-256), download to `~/.nexus/models/voice/`, ≤ 256 MiB, interprocess lock, only verified files activate |
| `voice/engine.py` | `KestrelEngine`: Kestrel 0.8.0 `ParakeetTdtRuntime` on a verified local checkpoint (model `moondream/parakeet-redux`) |
| `voice/audio.py` | strict WAV validation: mono, 16 kHz, PCM16, ≥ 0.2 s, ≤ `voice.max_seconds` (≤ 120) |
| `voice/model.py` | `VoiceState`, `TranscribeResult`, `VoiceError` |
| `host_support/voice.py` | bounded, redacted host dispatch and Doctor projection |
| `ui_support/voice_capture.py`, `tui_voice.py`; `ui/web/js/voice.js`, `voice-worklet.js` | TUI and browser capture and consent flow |

## Flow

1. **Install:** normal Nexus installation includes `moondream==2.4.0` and
   `sounddevice`; source checkouts use `uv sync`. The
   model (~179 MB) is **not** bundled.
2. **Consent:** the first use shows a confirmation dialog in the TUI and web;
   `nexus voice download` is itself an explicit action. `VoicePrepare` carries no
   consent token, so UI consent is a client-flow contract, not host-enforced.
3. **Prepare:** the daemon may warm a *verified* cache at startup but always passes
   `allow_download=False`; ordinary startup and transcription never download.
   An absent model stays cached-only.
4. **Capture:** clients send bounded mono 16 kHz PCM16 WAV (`VoiceTranscribe`; the
   browser uses `POST /v1/web/voice`, 8 MiB cap). The transcript is inserted as
   editable composer text; `auto_send` is off by default.
5. **Cancel/remove:** `VoiceCancel`, `VoiceRemove`, `nexus voice remove`.

Config: `[voice]` (`enabled`, `autoload` false, `max_seconds`, `device`
`auto|cpu|mps|cuda`, `auto_send`, `unload_after_minutes`); `model` and `revision`
are pinned to the one trusted manifest. `NEXUS_VOICE=off` disables it.

## Decisions and caveats

- The adapter uses Kestrel's internal `ParakeetTdtRuntime`, **not** the high-level
  Photon runtime, because Photon emits telemetry with no discovered opt-out. The
  internal API is version-sensitive.
- Real inference, platform support, benchmarks and network behavior are **not
  verified**. Do not describe the path as proven offline.
- The model weights are CC-BY-4.0; keep the attribution in `nexus/voice/NOTICE`.
- UI behavior (orange dot only while recording, any key stops, `Esc` discards,
  no auto "loading/ready" labels): [surfaces.md](surfaces.md#dictation).

## Adding or replacing an engine

Implement the `Engine` protocol (`load`, `transcribe(bytes)`, `close`) in
`nexus/voice/`, keep dependency imports lazy, preserve the bounded WAV,
serialized worker, model-store and host-command boundaries, and cover it with a
fake-engine unit test. No UI may import the inference library or read the cache.
Tests: `tests/test_voice_*.py`, `test_tui_voice.py`.

The Textual voice dialog shows only the actions for its current phase. Runtime
errors and unsupported installations show the host's message and Retry, even
before preparation is requested; a loaded model offers Start dictation and
replaces the download invitation.
