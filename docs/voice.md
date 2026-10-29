# Voice

Adapted from old Nexus `docs/voice.md`. Models come from the standard Hugging Face
cache (`$HF_HUB_CACHE`, `$HF_HOME/hub`, `$XDG_CACHE_HOME/huggingface/hub`, then
`~/.cache/huggingface/hub`). Nexus keeps no copy of its own. The runtime is the
optional extra: `nexus voice init` (ptts, numpy, Hugging Face Hub, torch,
Transformers, constriction and librosa).

Defaults, changeable in `config.toml`: `[voice] stt_model =
"ConwayResearch/Underdog-Bark-3.0-Clean"`, `[speech] tts_model =
"gradium/phonon-31m-en"`.

## Status

`GET /api/voice` returns `{stt, tts}`, each `{state, repo, revision, cache_path,
size, error}`. `state` is `unsupported`, `runtime_missing`, `absent`,
`downloading`, `ready`, `loaded` or `error`. It never loads a model or downloads
(`nexus/voice/hf_cache.py`, `status.py`). STT uses two pinned snapshots: the
Underdog Bark weight container and NVIDIA's Parakeet processor files. Their
manifest sizes and SHA-256 hashes are checked before a download is reported as
ready. Bark inference is supported on Apple Silicon and glibc Linux x86-64.

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

The Bark engine is not imported or loaded at daemon startup. First transcription
loads it from local snapshots using `local_files_only=True`; inference is
serialized on one worker thread. After 10 idle minutes the engine is closed.
Errors include 409 `voice_busy`/`voice_model_not_cached` and 422
`voice_dependency_missing`/`voice_model_invalid`/`voice_load_failed`.

## Speech

`POST /api/voice/speak {session_id}` reads the final text of the session's latest
completed turn (thinking and tool calls are never spoken) and streams `audio/wav`
(mono PCM16; the header sizes are `0xFFFFFFFF` because the length is not known).
The client plays it. The daemon runs a warm `ptts` worker in a child process,
offline (`HF_HUB_OFFLINE=1`), unloaded after 10 idle minutes. Errors are JSON
`{error, code}`: 409 `speech_busy`/`speech_turn_active`, 404 `speech_no_answer`,
422 for the rest (`speech_model_not_cached`, `speech_dependency_missing`, …).
`POST /api/voice/speak/stop` stops the current answer; the worker stays loaded.

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
progress; `runtime_missing` and `unsupported` show the status error as a toast. Otherwise
`nexus-tui/src/dictation.rs` captures with `cpal` on its own thread, downmixes and resamples
to 16 kHz mono PCM16, shows a waveform strip with start and stop cues, and sends paced
`?partial=1` previews (at least 0.4 s of new audio apart). Enter stops and inserts the final
transcript into the composer; Esc cancels and keeps the draft. Neither sends the prompt.
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
for consent before the download and shows progress in the composer; a denied microphone is
a toast. Not yet checked in a browser.

## Installing the runtime

The heavy packages (torch, transformers, librosa, constriction, ptts, numpy, Hugging Face
Hub) are the optional `voice` extra and are never installed with Nexus. `nexus voice init`
installs them into the environment Nexus runs from (`uv pip install --python <that python>`,
or `pip` without uv), prints the model status and asks you to restart the daemon
(`nexus daemon stop`). `nexus voice status` shows the status without installing. Models are
still downloaded only on request. In a development checkout `uv sync` removes these packages
again; use `uv sync --extra voice` there instead.

## Settings: Voice and speech

Both clients show the models in Settings → Voice and speech (manifest section `dictation`):
state, repo, size, cache path and any error (for example `nexus voice init`) per model, from
`GET /api/voice`. Download asks for consent first (repo, about 181 MB for speech to text and
65 MB for text to speech, from Hugging Face into the standard cache), then calls
`POST /api/voice/stt/download` or `/tts/download` and follows `voice.progress`. Remove
confirms (Cancel is the default) and calls `DELETE /api/voice/{stt|tts}`, which deletes only
that repo's folder from the cache; it is refused while a download runs or speech plays. Rows
are disabled with a reason when they make no sense in the current state. The settings are
read on open and after each action. Not yet checked against real downloads or in a browser.
