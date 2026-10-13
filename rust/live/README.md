# nexus-live

Audio and WebRTC helper for Nexus GPT Live calls (`/live`). A small process
that speaks JSONL on stdio. It holds **no credentials** and opens no network
connection except the WebRTC peer described by the SDP it is given; signaling
(offer to answer) is done by the Nexus daemon. Design: `plans/in-progress/GPT_LIVE_PLAN.md` section 4.2.

```sh
cargo build --release            # target/release/nexus-live
cargo test                       # offline; uses a fake audio backend
cargo build --no-default-features   # no cpal / no audio devices
```

## Protocol

One JSON object per line, UTF-8. stdin lines are limited to 64 KiB; a longer
line is skipped and answered with an `error` event. stderr is diagnostics only.

Commands (stdin):

| Command | Effect |
| --- | --- |
| `{"type":"offer"}` | Opens mic and speaker, creates the peer, gathers ICE fully (non-trickle) and replies with an `offer` event. |
| `{"type":"answer","sdp":"..."}` | Applies the remote answer. `ready` follows once the peer and data channel are connected. |
| `{"type":"mute","on":true}` | Mic frames are replaced by silence while muted. |
| `{"type":"stop"}` | Close mic, speaker and peer, emit `closed`, exit. Idempotent. |

Events (stdout):

| Event | Meaning |
| --- | --- |
| `{"type":"offer","sdp":"..."}` | Reply to `offer`. |
| `{"type":"ready"}` | Answer applied; peer connected and `oai-events` open. |
| `{"type":"levels","in":0.0,"out":0.0}` | Mic and speaker RMS (0 to 1), at most 10 Hz. Quiet periods are not repeated. |
| `{"type":"phase","phase":"listening"\|"speaking"}` | On change only. `speaking` while the assistant is audible plus a 300 ms hangover. |
| `{"type":"dc_error","message":"..."}` | An `error` event received on the data channel. All other data channel messages are ignored. |
| `{"type":"dropped","frames":3}` | 20 ms mic frames dropped because the 500 ms input ring overflowed. |
| `{"type":"error","message":"..."}` | A recoverable problem (bad command, over-long line). Setup failures also close the call. |
| `{"type":"closed","reason":"..."}` | Always the last line, exactly once. Reasons: `stop`, `stdin_eof`, `peer_failed`, `peer_closed`, `audio_error`, `offer_error`, `answer_rejected`, `peer_error`, `codec_error`. |

The helper exits when stdin reaches EOF (a crashed client closes the mic).

## Audio

Opus, 48 kHz, mono, 20 ms frames; capture and playback devices are resampled
linearly to and from their native rate. Echo handling is a half-duplex gate:
while the output RMS is above 0.01 (plus a 300 ms hangover) mic frames are
replaced by silence unless the mic RMS exceeds 0.12 (barge-in). Use headphones
for best results. Silence frames are still sent so the RTP clock keeps running.

Opus comes from `unsafe-libopus` (libopus transpiled to Rust), so the build
needs no CMake or C toolchain.

## Platforms

The `audio` cargo feature (default) pulls in `cpal`. On Linux it needs the ALSA
development headers (`libasound2-dev`, `alsa-lib-devel`) and `pkg-config` to
build; without them build with `--no-default-features`, which yields a helper
whose `offer` fails with an `audio_error` (the host reports `unsupported`).
Windows is not a target.

## Verified and not verified

Unit and loopback tests run offline (two in-process webrtc-rs peers prove the
data channel opens and Opus audio round-trips in both directions). Real
microphone/speaker I/O through cpal and a real OpenAI call are not covered by
tests.
