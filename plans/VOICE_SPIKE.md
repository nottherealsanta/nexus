# Voice runtime spike — findings and verification limits

Research and source inspection: 2026-09-30. No model weights were downloaded,
and no real-model transcription, performance benchmark, or runtime network
capture was performed. The facts below describe pinned source/configuration and
the implementation in this worktree; they do not verify a working inference
installation.

## Current implementation decision

Nexus pins `moondream==2.4.0` in the optional `voice` extra; its lock resolution
pins Kestrel 0.8.0. The runtime adapter is
`nexus.voice.engine.KestrelEngine`. It imports Kestrel's internal
`kestrel.models.parakeet_tdt.ParakeetTdtRuntime` and feeds it the locally verified
model directory. It does **not** construct high-level Photon
`InferenceEngine`: inspection of Kestrel 0.8.0 found startup and periodic
telemetry with no discovered opt-out. Bypassing that reporter is a
version-sensitive implementation choice, not evidence that inference is
network-free. Verify the exact installed runtime and observe its network behavior
before making an offline guarantee.

The adapter sets `RuntimeConfig.model` to `moondream/parakeet-redux` and
`model_path` to the **directory** containing all four checkpoint files. The
runtime receives WAV bytes through `forward("transcribe", …)` and returns a
mapping with `text`. This differs from the earlier proposed Photon call, which
passed a single safetensors path and a tokenizer path; that proposal is obsolete.

## Pinned model manifest

`nexus/voice/store.py` pins revision
`2bf128600aac4b16946f7ed8372e56117fe5e23b` and verifies each file's size and
SHA-256 before activating the directory:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `config.json` | 12,988 | `503c653b2e3bb788adbcb04f5abdee532d958686564081baeed133ff10143f6e` |
| `tokenizer.json` | 1,159,960 | `bd321b096832a3f270bd3b2a88823957920f1a5c5ada71114a26ea729d0cbe91` |
| `ternary.json` | 57,970 | `1221c6d3ce901ffe09c089da758a8db8b76189f80cff41c5afc244fc61e2051d` |
| `model.safetensors` | 177,774,490 | `78ec25733ee0d0c1586d1346fc86db9d0c2e436e3a8ab1d32a82d1bb8f848d21` |

The downloader uses HTTPS, a 256 MiB total cap, per-file hashes, an interprocess
lock, retries, and atomic activation. Its unit tests use fake/local data; the
manifest has not been validated by downloading the real repository files in
this work.

## Packaging, consent, and boundaries

- Install the optional runtime and TUI capture dependency with
  `uv sync --extra voice` (or install the package's `[voice]` extra). The normal
  install does not include Torch/Kestrel or `sounddevice`.
- First use in either UI requires explicit confirmation before a missing model
  can download. The dialog remains during preparation and until the ready
  acknowledgment. Daemon startup and an absent-model transcription request are
  cached-only; neither initiates a download. `voice.autoload` defaults false.
  `VoicePrepare` has no host-issued consent token; the UI confirmation is a
  client-flow contract, while `nexus voice download` is an explicit user action.
- Device selection is `auto` (CUDA, then MPS, then CPU), or configured
  explicitly. Wheel/runtime coverage and real performance on these devices have
  not been established here.
- The TUI and browser capture mono 16 kHz PCM16 WAV locally. The host validates
  and bounds audio; inference is serialized in the daemon; transcript text is
  returned for composer insertion and is not part of session records.
- Model metadata identifies the model as CC-BY-4.0; retain the packaged
  attribution in `nexus/voice/NOTICE` when redistributing.

## Required verification before claiming a production voice path

Install the pinned extra on supported platforms; explicitly approve a real model
download; verify all four files against the manifest; load and transcribe a
known clip on CPU and MPS (and CUDA where supported); exercise UI capture and
permission prompts; benchmark real-time factor; and inspect runtime network
traffic, including startup, load, inference, and shutdown. No such real-model,
microphone, benchmark, or network verification is claimed by this spike.

## Sources

- [Moondream Python SDK](https://github.com/m87-labs/moondream-python) and
  [PyPI release 2.4.0](https://pypi.org/project/moondream/2.4.0/).
- [Kestrel source](https://github.com/m87-labs/kestrel) and its pinned
  `RuntimeConfig` / `ParakeetTdtRuntime` implementation (0.8.0).
- [Parakeet Redux model repository](https://huggingface.co/moondream/parakeet-redux)
  and [pinned revision](https://huggingface.co/moondream/parakeet-redux/tree/2bf128600aac4b16946f7ed8372e56117fe5e23b).
- [Model card and license](https://huggingface.co/moondream/parakeet-redux).
