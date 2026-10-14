# Dictation and GPT Live on iOS (proposed)

Nothing is implemented. Both features depend on host features that are themselves
partly unverified: dictation inference and the Live service were never exercised
on real hardware/service ([voice.md](../../docs/voice.md), [live.md](../../docs/live.md)).
**Real service behavior and iOS audio in the background are unverified; do not
promise always-on voice and do not plan APNs in the first release.**

## Dictation

Mic capture is local on the iPhone; transcription runs on the **host** (**current**
engine: local model, [voice.md](../../docs/voice.md)). Text is inserted into the
composer as editable draft; nothing auto-sends unless the user chose it.

| Item | Current host contract | iOS plan |
| --- | --- | --- |
| Audio format | `voice/audio.py`: mono, 16 kHz, PCM16 WAV, >= 0.2 s, <= `[voice] max_seconds` (<= 120, default 120) | `AVAudioEngine` capture, resample to mono 16 kHz PCM16 WAV on device |
| Command | `VoiceTranscribe(audio: bytes, request_id, session, partial)` | do **not** send through the generic command route |
| Size | generic command body 1 MiB (`DEFAULT_MAX_BODY_BYTES`); browser voice route 8 MiB (`MAX_WEB_VOICE_BODY_BYTES`). 120 s of 16 kHz PCM16 is about 3.84 MB, over 1 MiB | proposed authenticated **binary upload** route in the gateway, reusing the 8 MiB cap and the host's WAV validation ([http_sse.py](../../nexus/host/transports/http_sse.py), [web.md](../../docs/web.md)) |
| Preview | `partial=True` re-sends the growing recording; host answers `voice_busy` immediately if busy | P3 option; start with final-only, add previews if latency is acceptable (**unverified**) |
| Retention | host: transient | no raw audio retained by default on phone; spool file in a protected temp dir, **deleted** on success, cancel, error, and next launch sweep |
| Consent | UI-enforced only today (`VoicePrepare` carries no token) | first-use screen: mic permission purpose string, "audio is sent to your Nexus host (not a cloud service)", limits, how to discard |
| Model | not bundled; `nexus voice init/download` on host | app shows host `VoiceStatus`; no download from the phone in P3 |

Cancel maps to `VoiceCancel`. Failed or empty transcription never sends. Handle
interruptions (call, Siri) by finalizing or discarding visibly.

## GPT Live (experimental, P4)

Architecture mirrors the existing client split ([live.md](../../docs/live.md)):

```
iPhone mic/speaker <-WebRTC media-> OpenAI realtime        (media never via Nexus host)
iPhone --LiveStart(offer_sdp)--> gateway --> workspace daemon --> OpenAI signaling
        <--answer_sdp--                                  daemon <-> OpenAI sideband WebSocket
```

- The app creates the **local WebRTC offer** and applies the answer. `LiveStart`
  accepts `offer_sdp` up to 64 KiB (**current**) and returns `LiveStartResult(call_id, answer_sdp, voice)`.
- **Credentials never reach the phone** (ChatGPT OAuth stays in the daemon).
  Mic audio goes directly from phone to the provider; the daemon holds only the
  sideband and the delegation bridge.
- The daemon accepts `LiveHelperEvent` kinds `ready, levels, phase, dc_error, dropped,
  closed` (`HELPER_KINDS`, **current**; unknown kinds are rejected with `live_bad_event`).
  The existing helper forwards only `phase` (listening/speaking), `dc_error`, `dropped`
  (batched) and `closed`. `ready` and `levels` are not forwarded by the existing
  helper; the daemon keeps the last levels in memory only and does not persist
  them (`live/manager.py`, **current**). The phone should keep levels local.
- **One active call per workspace daemon** (`live_busy` names the owner session,
  **current**); the app checks `LiveStatus` first and offers to open the owning session.
- **Approvals on screen only.** A spoken "yes" is never a permission
  (**current**). The phone shows the normal approval prompt.
- **Consent**: `LiveConsent` is recorded once per machine by the host and
  `LiveStart` is refused without it (**current**); the phone shows the same notice
  and, separately, asks iOS microphone permission with a purpose string.
- Calls end on stop, helper loss, deadline (`max_minutes`, default 30), session
  deletion, daemon shutdown, or **no attached view for the detach timeout** (30 s
  default `detach_timeout` in `LiveManager`, **current**). Reconnect after loss
  refetches authoritative state; the call itself is not assumed to survive.
- Ending a call never cancels the agent's running turn.

### Backgrounding and interruptions (unverified on iOS)

- Background audio entitlement, WebRTC under lock screen/app switch, route
  changes (AirPods, speaker), phone calls, Siri and other-audio interruptions must
  be tested on devices before any claim. Design assumption: the call may end when
  backgrounded; the UI says so before starting and shows an honest "call ended"
  state with the reason, never a silent failure.
- Last-viewer disconnect: the session's unattended policy applies to pending
  approvals (**current** default deny; configurable via `on_unattended`,
  [sessions.md](../../docs/sessions.md)). Other viewers may remain, and a custom
  policy may differ, so do not tell users "backgrounding always denies".
- Existing 30 s grace relates to *no attached view at all*; a phone backgrounded
  while a Mac client is attached does not trigger it.
- WebRTC distribution (library choice, binary size, license, App Review) is an
  open blocker ([DECISIONS](DECISIONS.md)).

## Privacy summary shown to users

Dictation: audio goes phone → your host only. Live: audio goes phone → OpenAI
through your ChatGPT account; the agent's progress and answers (possibly file
contents/command output) are sent to OpenAI by the daemon ([security.md](../../docs/security.md#gpt-live-data-sent-to-openai-and-consent)).
