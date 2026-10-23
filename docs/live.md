# GPT Live voice calls (`nexus/live/`)

Status: **M1 (daemon core), M2 (delegation bridge), M3 (the `nexus-live` helper), M4
(the native Ratatui client), and M5 (GPUI desktop and headless `nexus live`) are
implemented and tested offline. Nothing has been exercised against the real service,
a real microphone or a real call: not verified. The cause of a `peer_failed` disconnect cannot be identified from the screenshot alone;
the helper now logs ICE state names only, without sensitive data. This logging has not
been verified in a real call. `rust/live/src/peer.rs` uses a different WebRTC API from
reference `codex-rs/voice-host/src/transport.rs` (`APIBuilder` versus
`PeerConnectionBuilder`/`SettingEngine`); do not infer equivalent runtime behavior.** The desktop client draws the call
bar and card and routes `Ctrl+X D` (see [desktop.md](desktop.md#gpt-live-voice-calls));
a native-harness render check passed, but screenshot review has not run. Plan:
`plans/done/GPT_LIVE_PLAN.md`; protocol observations and citations:
`plans/done/GPT_LIVE_PROTOCOL_NOTES.md`.

A real-time spoken conversation with OpenAI's `gpt-live-1-codex` model. The voice
model only talks. **The Nexus session the user already has open does the work**
with its own model, tools and permissions: a spoken request becomes an ordinary
user message in that session. The endpoints are undocumented and re-implemented
from two MIT-licensed reference integrations (`nexus/live/NOTICE`); the feature is
experimental and may stop working without notice.

```
 mic/speaker <-> nexus-live helper <-> (WebRTC) <-> OpenAI realtime      (M3, client side)
                    ^ SDP offer/answer only                ^ sideband WebSocket
                 client --LiveStart/Mute/Stop/HelperEvent--> daemon: LiveManager
                                                              |  LiveBridge
                                                       HostFacade.enqueue / session events
```

## Files

| Path | Owns |
| --- | --- |
| `live/protocol.py` | frozen shapes: call request, inbound events (`parse_event` is total, unrecognised frames become `Unknown`), outbound appends, `chunk_utf8` (<= 500 UTF-8 bytes, never inside a code point) |
| `live/transport.py` | `LiveTransport` protocol; `CodexLiveTransport`: signaling POST, call id from `Location`, sideband WebSocket, serialised sends, `session.close` on stop |
| `live/manager.py` | `LiveManager`: one call per daemon, state machine, deadline, detach watchdog, events, instructions and continuity |
| `live/bridge.py` | `LiveBridge`: delegations in, progress/final/notice appends out |
| `live/prompts/instructions.md` | bundled voice instructions |
| `host_support/live.py` | `dispatch_live` and `FacadeLiveHost` (the manager's per-call seam to the session layer) |

`nexus/live/` is a manager beside `nexus/voice/`: it imports no session, runtime or
host code. `core/loop.py` knows nothing about it. The host layer injects a
`LiveHost` for each call, which is how events are persisted and delegations are
enqueued.

## Flow

1. A client's audio helper creates an SDP offer and sends `LiveStart(session,
   offer_sdp, voice, new_conversation)` (offer <= 64 KiB).
2. The daemon renders the instructions, records `live.started` (call id, voice,
   the **full instructions text** and how many earlier turns were carried), then
   POSTs `{"sdp", "session"}` to the calls endpoint with the ChatGPT OAuth headers
   from `CodexOAuthManager`. The SDP answer is the response body; the call id is the
   `Location` header. Only `LiveStartResult(call_id, answer_sdp, voice)` goes back to
   the client. Credentials never leave the daemon.
3. The daemon opens the sideband WebSocket (`wss://api.openai.com/v1/live/<id>`,
   same headers). `SidebandOpen` moves the state `connecting -> active`.
4. Final spoken turns (`turn.done`) become `live.transcript`; partial text becomes
   a coalesced caption (`live.transcript.delta`).
5. `delegation.created` goes to the bridge (below). Everything the daemon sends to
   the voice model is recorded as `live.appended`, text exactly as sent. Spoken turns
   are bounded at 200,000 characters with a visible omission notice so truncation is
   never silent; this is a resource cap, not an unlimited request guarantee.
6. The call ends by `LiveStop`, helper exit, sideband loss, the voice service
   closing it, the deadline, no attached view for 30 s, the session being deleted,
   or daemon shutdown. Every path converges on one guarded `_finish` that sends
   `session.close` (<= 1 s), closes the socket and records **exactly one
   `live.ended`** per `live.started`. Ending a call never cancels the agent's
   running turn. When the session itself was deleted there is nothing left to
record into, so that one `live.ended` is logged instead of persisted. The facade
ends the call before it closes sessions on shutdown, so the event is durable.

States: `idle -> connecting -> active <-> muted <-> paused -> closing -> idle`. A second
`LiveStart` fails with `live_busy` naming the owning session. Mute is local to the
client's helper; the daemon records it so every view agrees (no sideband message
exists for it).

## Client flow (Ratatui, M4)

`/live` (or `Ctrl+X D`; `Ctrl+X L` was already the details sidebar) toggles a call. In the
native clients the call is **always attached to the manager session**, whichever session is
on screen (plan 02 §10, offline-tested only): the panel follows the manager itself, shows its
approvals while another session is in front, and the composer keeps sending to the displayed
session (see `ratatui-parity.md`, "Live is always on the manager"). The step list below
predates this and describes a call on the shown session. Code: `ui/ratatui/live.py` (bridge state) over
`ui_support/live_helper.py` (stdlib only; the desktop client and a CLI reuse it).

1. `LiveStatus`. Not available (`disabled`, `unsupported`, `no_credential`, `busy`)
   shows a panel with the host's message and the fix (`nexus auth codex login`,
   `uv tool install --upgrade --force nexus-harness` then restart the daemon, the session
   that owns a busy call).
2. The `nexus-live` binary must exist (`NEXUS_LIVE_BINARY`, the venv's bin, `rust/live/target`,
   `PATH`); otherwise an `unsupported` panel says to upgrade or reinstall the normal Nexus default package, then restart the daemon; source builds are only in developer notes. Asked before consent.
3. First use only (and again whenever the notice version changes; it is 2 since the call
   started receiving session context): a consent dialog says that microphone audio,
   summaries of the agent's work and a bounded excerpt of the session conversation go to
   OpenAI through the user's ChatGPT account. Agreeing calls
   `LiveConsent(accept=True)`, a record in `<nexus home>/live-consent.json`
   (once per machine, every client; `/live revoke` removes it). The **daemon refuses** a
   `LiveStart` until that record exists (`live_consent_required`, no signaling and no
   sideband opened), so no client can skip the notice. Tested offline
   (`tests/test_live_consent.py`); not verified against a real call.
4. The helper is spawned; `offer` -> `LiveStart(offer_sdp)` -> `answer` -> `ready`. Any
   failure stops the helper and, if the daemon had accepted, sends `LiveStop`, and shows
   the host's redacted message plus the fix (macOS Privacy > Microphone for the terminal
   when the helper cannot open the mic). Start and stop play the dictation cues.
5. The helper's `closed`, `dc_error`, `dropped` and `phase` are forwarded with
   `LiveHelperEvent` (dropped counts at most once per second); `levels` stay local
   for the call bar. Our own stop is not reported back as `closed`.
6. A watch task ends the client side when the helper exits (it tells the daemon,
   shows the reason and the microphone fix), when the call's `live.ended` arrives
   (deadline, another client, session deleted) or when `LiveStatus` shows the daemon
   has no call (checked every 5 s). Closing the shell stops the call.

| Command | Effect |
| --- | --- |
| `/live` | start, or hang up when a call is open |
| `/live stop` | hang up (idempotent) |
| `/live pause` | pause or resume (`LivePause`; `Ctrl+X P`), see [Pause vs stop](#pause-vs-stop) |
| `/live resume` | resume (idempotent); after a pause ran out, `/live pause` or `Ctrl+X P` starts a new call |
| `/live mute` | toggle the microphone (`LiveMute`; the helper mutes, the daemon records it) |
| `/live new` | start a call without the carried-over turns (`new_conversation`) |
| `/live status` | availability, state, consent, helper installed, last error |
| `/live revoke` | withdraw the data-sharing consent |

**Call line (design overhaul step 11, P-LIV).** The composer carries nothing of the call. The
call's status line is the header row of the Live pane (below), drawn on `p.element`:
`LISTENING   ▁▃▅█▆▃▂▁  gpt-live-1-codex  /live stop      02:41`, then `↑ N more` and the expand
button at the right. The phase is in caps, bold and padded to 12 cells (`CONNECTING` muted,
`LISTENING` blue, `WORKING` purple, `SPEAKING` accent, `MUTED`/`PAUSED`/`ENDED` quiet), followed
by that phase's motion band (`rust/tui/src/live_wave.rs`) and the clock right-aligned. The band is a
row of block-height bars `▁▂▃▄▅▆▇█`, up to 14 cells (fewer when the line is narrow, none below 4),
a pure function of the 30 Hz activity clock so it moves smoothly (no dots, no braille spinner):
- connecting / working: a narrow bright shimmer travelling left to right (muted while connecting,
  purple while working; working adds `agent turn #N running`, `N` from the newest
  `Sent to agent … → turn #N` row, `agent running` when no row names a turn; the label is dropped,
  not clipped, when the line cannot hold it beside at least 4 cells, and the band is 6 cells beside it);
- listening: a calm breathing swell (about 5 s, never above roughly 30% height) in blue. The
  microphone lifts it: the newest history on the right plus the live level, so the bars follow the
  user's voice. The snapshot carries only the current `in` level, so the client keeps a history of
  the newest samples (`Cache.mic`, one per 125 ms tick) and eases the levels per frame (quick
  attack, slow release); it is emptied when the call closes;
- speaking: a fuller two-component travelling wave in the accent colour with a soft gradient
  (full accent in the middle, dimmer at both edges), deeper as the eased output level rises;
- muted: a flat `▁` line then `mic off`; ended: a flat line; paused: `ends in mm:ss` text only.

Frames at 30 Hz are requested only while the call moves (`live_wave::wants_frames`: active, not
paused, not muted, not ended). With Settings → Appearance animations off (or `NO_COLOR`) every
state rests in a static pose (shimmer centred, speaking a still arch, listening the mic history
only, which is data). ASCII terminals (`NEXUS_ASCII=1`) use `_.:-=+*#` bars. Pieces drop by width, in this order: the `/live stop` (`Ctrl+X P resume` while
paused) hint, the model name, the `in <session>` note, the motion band, the clock; the phase is
truncated last. Text frames of every state: `artifacts/live-panel-anim/bands.txt`
(`cargo test dump_frames -- --ignored`). Not verified in a real terminal with a real call.
Typing during a call is ordinary steering.
When the call is on another session or has no rows yet (still connecting) there is no row
list, but the microphone must stay visible: the pane is then the header alone, one row above
the composer, `LISTENING   ▁▁▁▁▁▁▁▁  in <session>  /live stop    00:12` (a connecting call reads
`CONNECTING  ●○○ … 00:00`). No expand button, no scrolling. Tested in `rust/tui/src/live_bar.rs`
and the `live_pane_tests` of `render.rs`; the earlier `● Live · phase · mm:ss   mic/spk` meters
are gone (not verified in a real terminal or call).

**Live pane (terminal).** The native terminal draws the open call's card rows in a pane
under the composer card and above the bottom bar (the activity meter row), not in the timeline; the
vertical order is transcript, composer, Live pane, bottom bar (`Regions.live`, `Regions.bottom_bar`).
The pane's rows come out of the transcript, never the composer, and are capped so the transcript
keeps one row (a terminal too short for that shows no pane; not verified on real tiny terminals).
**Opening motion** (`rust/tui/src/live_anim.rs`): the pane grows from 0 to its full height over
260 ms (ease-out cubic) as if sliding out from beneath the composer: the header appears first, the
text fades in with the height, and an accent band sweeps along the header for 560 ms. Closing reverses
in 140 ms (ease-in) from a ghost of the call's last state drawn as `ENDED`. The clock is the TUI's
frame loop (`Instant` per pass, the same loop as spinners and folds); `LiveAnim::apply` runs each
pass, returns whether another frame is needed, and stops requesting frames once the motion and its
settled frame are drawn. With animations off (Settings, Animate activity; `NO_COLOR`; `NEXUS_ASCII`)
the pane jumps to its final state. Layout follows the revealed rows (`Snapshot.live_reveal`, local),
so the composer rises as the pane grows and the bottom bar never moves. Covered by Rust unit tests;
a real-terminal capture of the motion is not verified. Data: (`Live.pane`; `bar()["detail"]`,
`collapsed`, `operation`). The pane is 6 rows, the call-line header and five content rows, and follows the newest rows. The mouse wheel
over it scrolls. Clicking a row opens or closes the card (the `live_toggle` operation, the
same as the card). A button at the header's right (` ⤢ expand ` / ` ⤡ collapse `, beside the `↑ N more` notice) expands it to nearly full screen, up to
just below the top bar (the timeline keeps one row), so the transcript can be read; the same button collapses it again.
The expanded state is local to the terminal client and resets when the call ends. Hidden
rows are announced as `↑ N more` or `↓ N newer`; clipping is never silent. Captions (`live_bar::captions`):
a partial `You…` caption is italic `p.muted` and final `you` text is `p.text`; the `Voice…` caption in
progress reads word by word, spoken words `p.text` and the newest word `p.accent` bold underlined;
`Sent to agent (mode)` is a chip, ` → sent to agent ` (bold, `p.background` on `p.warning`) then
` turn #N · mode ` (`p.text` on `p.element`) followed by the text, drawn in place with no slide. The
host sends no playback position (not verified), so the newest word of the delta stands for the
current one and upcoming words are not dimmed ahead of time. Other rows keep their look. The pane appears as
soon as the daemon has recorded `live.started`, even before this client has the start result:
`Live._view_call()` then matches the newest non-ended call of the shown session that began after
this start, so the old timeline card never flashes. After the call ends the card joins the timeline.

**Timeline card.** Each call renders as one labelled `Live` card (native block kind `live`,
rows from `ui_support/live_card.py`), placed before the first turn that started after the
call began, so a delegation's turn follows the call that caused it. Rows: `Started` (the
instructions source, carried-over turns; the full instructions text when opened),
`You` / `Voice` (final captions, plus the streaming partial while active), `Sent to agent
(queue|steer)` with `-> turn #N` linking the user message it created, `Told voice [final|notice|commentary]`
(text exactly as sent), non-fatal `Error` rows and an `Ended` row (reason, duration, redacted
error). Collapsed by default: commentary becomes one counted row, long text is clipped with
"+N more characters", and the instructions are summarised; click the card (or Enter on it) to
open everything. Nothing is dropped. A user message created by a delegation shows
`via voice` beside its turn number. The card is rebuilt from the durable events, so a
reconnect or replay shows the same thing (`tests/test_ratatui_live.py`). In the terminal the
open call is left out of the timeline while it is open, and its card joins the timeline when
the call ends. The desktop client (`shell.live.pane = False`) keeps the card in the timeline
during the call and has no pane.

## Pause vs stop

| | Stop | Pause |
| --- | --- | --- |
| Keys / commands | `Ctrl+X D` (also starts a call), `/live`, `/live stop` | `Ctrl+X P`, `/live pause` (toggle), `/live resume` |
| Call | ends: `live.ended`, helper and socket closed, billing stops | stays open, bounded by `[live].pause_seconds` (default 120) |
| Microphone | released | held open but sends silence |
| Voice audio | stops | dropped by the helper (what it was still sending is discarded) |
| Agent turns | untouched | untouched; its results for the voice are **held** |
| Resume | a new call (`Ctrl+X D`); the transcript is carried over | `Ctrl+X P` again: held results are spoken in order |

**Why `Ctrl+X P` and not `Ctrl+X F`.** `F` is already Fork session in the terminal client
(`rust/tui/src/main.rs` leader `f`, `LEADER_SHORTCUTS`) and the desktop client (`ctrl-x f`), so it was not free.
`P` was unbound in both (desktop `Ctrl+P` is the palette, which does not collide with the chord).

**What Codex does.** Nothing named pause. Its voice mode has `toggle_voice` and `toggle_voice_mute`
(`tui/src/keymap.rs`), and mute only keeps the WebRTC/RTP track alive with generated silence: "Muted capture
sends generated silence to keep the peer alive" (`voice-host/src/audio_track.rs`, see
`plans/done/GPT_LIVE_CODEX_REFERENCE.md` sections 1.5 and 6). So Codex's closest thing is mute with the
socket and session open. Nexus's `mute` is the same and stays as it was.

**Billing (the reason pause is bounded).** Source: OpenAI's Realtime costs guide,
<https://developers.openai.com/api/docs/guides/realtime-costs>, read 2026-10-16 through a summarising fetch
(quotes below are from that summary of the page, not a direct read of the HTML).

- GPT Live voice sessions are "billed per second at the current model rate", not rounded up to a minute.
  *Confident: stated plainly in the docs.*
- "Active session time includes time when the user speaks, the assistant speaks, both are silent, or the
  backend is working." *Confident.* Silence and idle time are billed.
- "Muting microphone input does not close the session." *Confident*; the guide's example says closing "saves
  $0.05 per minute of idle voice time" (an illustrative rate, not the real one) and points to an inactivity
  timeout and a close-and-resume workflow. So **a muted or paused call that stays open keeps billing.**
- A WebRTC session request "bills 15 seconds of voice duration while the session initializes", credited
  against duration charges once it runs. *Fairly confident:* resuming by starting a new call costs no extra
  beyond that credited initial charge.
- The older token-based Realtime models bill per response and say empty audio is not counted; that page section
  does **not** apply to GPT Live and is not relied on.
- Whether the real service meters differently for a paused sideband, what the real per-minute rate is, whether
  the 60-minute cap that the Realtime docs state for ordinary Realtime sessions applies to `gpt-live-1-codex`,
  and how these calls appear on a ChatGPT-account (OAuth) bill rather than an API key: **not verified**. This
  integration uses the ChatGPT sign-in, and the guide describes API billing.

**Design.** A pause that keeps the socket open is not free, and Nexus cannot close the socket and keep the call:
the audio is WebRTC and the sideband only carries events (media dies with the peer; a sideband rejoin of a gone
call is a 404, see the recovery notes above), and there is no resumable call id. So:

1. **Paused (<= `pause_seconds`).** The client's helper receives `pause`: it sends Opus silence (the RTP clock and
   ICE consent keepalive keep running, as for mute) and clears the playback ring every 20 ms. The daemon sets the
   state `paused`, phase `paused` (durable `live.phase`), and **holds** voice-bound updates: nothing may make the
   voice speak while paused. At most 8 final/notice updates are kept (oldest dropped), quiet `commentary` is dropped
   as stale, and the drops are announced as a `live.error` on resume. The agent's turns keep running and are visible
   as usual. A late `delegation.created` still goes to the agent (an ordinary delegation).
2. **Resume.** The timer is cancelled and held updates are sent in order (the voice then speaks them).
3. **Bounded.** After `pause_seconds` the call ends like any other end (`live.ended`, reason `pause_timeout`),
   so billing stops. `Ctrl+X P` (or `/live pause`) in that client then starts a new call, which the existing
   handoff carries over: session context and the last `carry_turns` spoken turns are in the instructions
   ([Instructions and continuity](#instructions-and-continuity)). The deadline (`max_minutes`) keeps running
   during a pause.
4. **Not done:** cancelling a response the voice is mid-way through producing (the sideband has no verified cancel
   message, so the helper just discards the audio; the model may finish generating it, billed as session time
   anyway), and any client-side `input_audio_buffer` clearing (there is none: audio is WebRTC, not appended).

The trade-off: a short pause (a phone call, a cough) resumes instantly with the same voice context and costs only
the seconds it lasts; a long one costs at most `pause_seconds` of idle time and then behaves like stop plus start.
Setting `pause_seconds = 5` makes pause almost a stop; `3600` keeps a quiet call open for an hour at full price.

**Where it shows.** The Live pane header reads `PAUSED      ends in mm:ss  Ctrl+X P resume   mm:ss` (the
countdown is the time to the end of the pause; `Ctrl+X P resume` replaces the `/live stop` hint), the desktop call bar shows `PAUSED mm:ss`
and a resume button, `/live status` has a `Paused` row, and `LiveStatusResult` carries `paused` and
`pause_remaining_s`. State is the daemon's, so every view agrees and a reconnect sees it. Tested offline
(`tests/test_live_manager.py`, `test_ratatui_live.py`, `test_ui_support_live_helper.py`, Rust `cargo test`);
no real call: not verified.

## Audio buffers and freshness (`rust/live/src/dsp.rs`, `audio.rs`)

Live is conversational, so stale audio is worse than a small gap. Both rings between the call pipeline and the
sound devices (mono 48 kHz i16) are therefore short and **keep the newest samples** when they overflow:

| Ring | Bound | On overflow |
|---|---|---|
| capture (mic -> pipeline) | `INPUT_RING_SAMPLES` = 200 ms | `push_keep_latest`: oldest samples evicted, counted as drops |
| playback (decoder -> speaker) | `PLAYBACK_SAMPLES` = 200 ms | `push_drop_oldest`: oldest samples evicted |

- Earlier the capture ring held 500 ms and `push` kept the *oldest* audio, discarding new samples when the pipeline
  lagged, so a lagging consumer could transmit mic audio up to that old. The playback ring held 1 s. Neither
  has been measured against a real call (not verified); the bounds follow from the freshness policy.
- 200 ms is meant to ride out normal scheduling jitter (the pipeline drains the mic in 20 ms frames) without audible
  chopping, while capping the worst-case added latency. If drops show up in normal use, raise the constants rather
  than switching policy back.
- Dropped capture audio is reported to the pipeline in whole 20 ms frames (`take_dropped_frames`).
- `SampleRing::push` (keep oldest, drop newest) is retained for callers that want it; capture no longer uses it.
- The speaker callback asks the ring for as many samples as the device requested in that callback (scaled to the
  device sample rate), so large device buffers are not padded with silence while audio waits. A real underrun still
  plays silence.

## Delegation bridge (M2)

A delegation is **untrusted input**, exactly like a typed prompt.

What GPT Live can and cannot do: the session offers a client delegation
(`delegation: {type: client}`) and no documented native tool capability, and Nexus sends
no tool schemas to it. So Live never calls Nexus tools itself, and the voice model cannot
author its own wording for the agent: in the recorded call `session-1ce5e575` every
`live.delegated` text equalled the user's final spoken transcript word for word. The public
GPT-Live docs go further and say a client delegation event carries no task text at all, and
Codex has the same shape (no tools; the delegation content is the user's input). So Nexus
follows Codex's handoff instead of asking the voice model to rewrite requests (see
[decisions.md](decisions.md) and `plans/done/GPT_LIVE_CODEX_REFERENCE.md` section 3).

**What the agent receives.** One ordinary user message, plain text, with the request, a
labelled transcript of both speakers since the previous delegation of the call (or call
start), and a note that it was speech:

```
Voice request
Check the README.

Spoken conversation since the previous request
You: hello there
Voice: Hi, what can I do?
You: um check the readme

Note: this request came from a voice call. The text is speech recognition output and may
contain errors or be unpunctuated; work out what the user meant and say so if unsure. Your
final answer is read back to the user by a voice assistant, so lead with a short
plain-language summary.
```

- The block is the message text itself, so the timeline (labelled "via voice") shows exactly
  what the agent saw. Nexus has no hidden-context channel for user messages and Live does not
  add one. Replay reproduces it, because it is the durable `input.queued` text.
- A trailing `You:` line equal to the request is dropped (it would repeat it). Speech still in
  progress when the delegation lands is included and is not queued again when it is finalised.
- Bounds are resource limits, not a request-length policy; a spoken request is never silently
  cut. Codex's 4 KiB bounds were too small for long requests, so Nexus uses: the request is
  delivered whole up to 200,000 characters (`MAX_DELEGATION_CHARS`, inside the 1 MiB sideband
  frame limit) and 1 MiB in the composed message; the background transcript keeps its last
  32 KiB. Each spoken turn handed over is bounded at 200,000 characters. Any clipping is
  announced inside the text (`[… N more characters omitted]`, `[… N earlier characters
  omitted]`, `[… earlier speech omitted]`), and a clipped request also records a durable
  `live.error`. A sideband frame over 1 MiB cannot be read at all; that records a `live.error`
  ("too large to read") instead of being dropped silently. When there is no delegation text,
  the transcript is the request and gets the request bound. The manager keeps at most 64
  turns between delegations. The caption/`live.transcript` rows keep their own 16,000-character
  display bound and set `truncated`; that does not affect what the agent receives.
- The assumed 200,000-character limit applies to the composed user message, including its
  voice note, transcript, and any omission notice; it is not a verified service-side limit.
  For oversized messages the earliest text is kept and ends with an exact-count notice such
  as `[… N characters of the spoken request omitted to fit the 200000-character message
  limit. This request is incomplete; ask the user to repeat the missing part.]`. A durable
  `live.error` also announces how many characters were omitted. Exactly 200,000 characters
  passes whole without a tail notice.
  The bound is deliberately kept: removing it would let one utterance exceed the sideband frame
  and memory limits.
- An empty or whitespace delegation uses the transcript as the request (heading "Voice request
  (taken from the spoken conversation)"). Only if both are empty is the request rejected.
- A fresh session is titled from the request alone, not from the wrapper.
- `live.delegated` keeps `text` (the delegation text, or the transcript when it was empty),
  `heard`, `source`, `verbatim`, and adds `transcript` (the transcript lines as sent),
  `transcript_omitted` and `input_omitted` (characters clipped; `input_omitted` is only the
  bridge's 1 MiB bound, a manager clip is in the text) and `from_transcript`.
- Whether the service's `delegation.created` content is ever the voice model's own wording is
  **not verified**. The bundled instructions no longer ask for a rewrite; they tell the model
  the service passes the user's words and the recent conversation on.

**What the voice model receives back.** The final answer is plain speakable text with no
label: a `session.context.append` with `channel: "speakable"` and no `delegation_item_id`,
what Codex's TUI sends for `gpt-live-1-codex` (client-managed handoffs,
`tui/src/app/tests/realtime_handoff_e2e.rs`). Chunked at 500 UTF-8 bytes; <= 12,000
characters, omission announced. Progress stays a quiet `delegation.context.append` on
`commentary`; failure, cancellation, merge and on-screen-approval notices stay
`delegation.context.append` on `speakable`. `live.appended` records the text exactly as sent,
the card channel (`final`/`notice`/`commentary`, unchanged) plus `wire` (the message type) and
`wire_channel`. Not verified against the real service: that the service accepts a final answer
without the delegation id and has the model speak it, and that it does not wait on the
delegation id.

| Situation | Behaviour |
| --- | --- |
| idle session | `HostFacade.enqueue(mode="queue")`: a new turn |
| a turn is running | `enqueue(mode="steer")`: consumed at the next model step |
| the user message | its `input.queued` event carries `origin = {"kind": "live", "delegation_id"}`; the text is the request block above, the turn is otherwise identical to a typed prompt, clients label it "via voice" |
| progress | `tool.requested` bursts become one `commentary` append at most every 5 s (`[live].progress`) |
| `turn.completed` | last visible assistant text, plain speakable `session.context.append` (<= 12,000 characters, the omission is announced) |
| `turn.failed` / `turn.cancelled` | a short notice append |
| several delegations in one turn | the last gets the final answer; earlier ones get a "merged into a later request" notice |
| `permission.requested` / `question.requested` | a notice that the user must answer **on screen**. The bridge never resolves, grants or auto-approves anything; a heard "yes" is never a permission |

The bridge watches the session through `Session.observe()`, a tap that is **not**
a viewer, so it can neither make an unattended session look attended nor change
the unattended approval policy. Sub-agent events are ignored.

### Delivery failures, reconnects and silence

Transport contract (implemented in `transport.py`, consumed by `manager.py`): the
transport waits a bounded time for a sideband reconnect before writing, recovers on its
own or calls `on_closed` terminally, and emits the synthetic `SidebandReconnected(gap_s)`
after a gap. `LiveError` codes from `send`: `live_not_connected` means definitely **not
sent**; `live_send_ambiguous` means the write failed and delivery is **unknown**. Neither
is ever replayed. Any other send error (`live_send_failed`) is fatal and ends the call
with `transport_error`.

Transport timing and credentials:

- Waiting for a connection (the lock, the initial connect or a reconnect) is bounded at
  `SEND_WAIT_TIMEOUT_S = 10` s and each write at 5 s; both failures are reported as above and
  never replayed. A message that was **never written** may wait for the reconnect inside that
  window (it covers the first backoff steps plus one open); Codex keeps such a message pending
  without limit, Nexus bounds it because the bridge appends under a per-call lock. A write that
  raised or timed out is delivery-ambiguous and is never replayed (Codex replays; Nexus does not).
- **Join (initial open):** an early `404` on the first sideband open is retried at 100, 300 and
  800 ms (`JOIN_404_DELAYS_S`, Codex: the call id can be returned before the call is active),
  then startup fails. Other failures use the existing 5-attempt, 200 ms exponential retry; any
  other HTTP refusal (401/403/429, 410, 400, ...) fails at once.
- **Reconnect after an unclean loss is not attempt-limited** (Codex-like). Attempt `n` waits
  `min(0.2 * 2^(n-1), 5)` s (`RECONNECT_BASE_DELAY_S`, `RECONNECT_MAX_DELAY_S`), then refreshes
  credentials (5 s) and opens (15 s). `n` counts consecutive failed opens and short-lived
  connections; a connection that lasted 30 s (`RECONNECT_STABLE_AFTER_S`) resets it. Recovery
  ends only on: HTTP `404`/`410` on the rejoin (call gone; `id was accepted earlier; no
  replacement call created`), an auth/forbidden/rate-limit refusal, any other non-retryable HTTP
  refusal, a credential refresh timeout, a clean server close (code 1000, seen by the reader or
  by a failed write: `remote_closed`, no reconnect), explicit stop, the call's own deadline
  (`max_minutes`), or `RECOVERY_GIVE_UP_S = 300` s of continuous failure (measured from the first
  drop of an unstable stretch, reset by a 30 s stable connection). The 5-minute cap is Nexus's own
  addition: Codex loops while the call is active, and AGENTS.md requires every loop to be bounded.
  It outlasts a network change or sleep/wake and still ends a dead call. The terminal detail reads
  `... recovery attempts used N, gap Xs; drop: <type> rcvd=.. sent=..`, plus `gave up after 300s
  of continuous failure` for the cap (redacted and bounded as before).
- **Heartbeat:** Nexus sends **no client WebSocket pings** (`ping_interval=None`), as Codex does.
  The `websockets` library still answers server pings automatically. Trade-off: a half-open
  socket (peer vanished with no FIN/RST) is no longer detected by a missed pong. It is noticed
  only by a read error, a write that fails or exceeds 5 s (`live_send_ambiguous`, then recovery),
  or the server closing; a write into a dead socket's kernel buffer can still appear to succeed
  until TCP gives up. Pings were removed because Codex never sends them and they were a
  candidate for the ~43 s drop (the confirmed cause is the media path, see
  [Root cause of the ~43 s drop](#root-cause-of-the-43-s-drop)). Disconnect reports append the
  exception type and `rcvd=`/`sent=` close code and reason (`none` when no frame). **Not verified
  against the real service**: whether the sideband server needs or tolerates the absence of client
  pings, and whether TCP keepalive should be added if silent dead sockets are seen.
- Every reconnect fetches **fresh credentials** from the auth manager first (the access
  token may have rotated); a new token is redacted like the first. An auth failure at
  that point ends the call at once instead of retrying.
- A terminal `sideband_lost` with `Could not open the sideband connection` means an
  already-open sideband failed to reconnect; initial setup failure is `startup_failed`.
  Open failures include the exception type (and the configured open timeout for
  timeouts), with credential redaction and bounded detail. An empty timeout message
  is not evidence of an auth failure. Reconnects remain bounded; commentary that was
  not sent, or whose delivery is uncertain, is never replayed automatically.
  (`sideband_lost` after a recovery is now a 404/410, an auth refusal, the 5-minute cap, or a
  non-retryable refusal; it no longer means "two attempts failed".)
- HTTP refusals retain their status across the connector boundary. Only 408 and
  5xx responses are eligible for the existing bounded retry policy; other HTTP
  errors (including 404/410, auth and rate limits) end opening/recovery immediately,
  without spending the remaining attempts. Network failures still use bounded
  recovery. A WebSocket refusal is labeled `sideband WebSocket upgrade rejected`,
  not `call creation failed`; bounded response-body detail is credential-redacted.
  Recovery counters include the one attempt that received the terminal response. The initial open alone retries a 404 (see **Join**).
- A 404 during recovery means the HTTP upgrade was refused, not simply a dropped
  socket. It may mean the call expired/disappeared or that the configured endpoint,
  provider deployment or protocol version no longer supports this route. The
  reverse-engineered protocol does **not** establish which, or promise that a 404
  will become recoverable. Earlier versions mislabeled upgrade errors as call
  creation failures and retried them, obscuring this distinction. A preceding
  `no close frame received or sent` describes the original drop, not why HTTP
  returned 404. Confirming the cause needs the service response and deployed
  endpoint/version/configuration; the elapsed time alone cannot establish expiry.
  Observed report (older build, 2-attempt budget): `Ended sideband_lost · 00:43 … upgrade
  rejected (404) … recovery attempts used 1/2, gap 6.1s; drop: no close frame received or sent`. The initial
  open had succeeded (the call ran 43 s), so the 404 came from the first reconnect
  with the same call id, URL and freshly fetched headers. Both reference integrations
  reuse that id for every attempt; the documented protocol has no rejoin call or
  short-lived URL/token to refresh, so there is nothing more correct to retry. The
  terminal text adds `id was accepted earlier; no replacement call created` for 404/410
  during recovery. **Known limitation / not verified**: whether the service ends the
  call when the sideband drops, expires it, or rejects a second sideband for the id
  needs a real-service trace (response body, timing, whether a second WebSocket on a
  live call works). If the call was already torn down by the media-path bug in
  [Root cause](#root-cause-of-the-43-s-drop), the 404 is a consequence, not a separate fault.
  Nexus does not automatically POST a replacement call: that would start a new
  semantic conversation and cannot safely restore unheard audio or uncertain
  commentary. Start a fresh live call explicitly after checking configuration.
- Outgoing JSON is ASCII-escaped, so text containing a lone surrogate cannot fail the
  UTF-8 encode of a healthy socket.
- Stopping is **best effort**: the call is marked terminal in the same loop turn as the
  cancellation of in-flight sends, so a send cancelled mid-write cannot start a reconnect.
  `session.close` is attempted (<= 1 s) only on a socket no write was cancelled or failed
  on; otherwise the socket is just closed locally. The server may not see an explicit close.
- Liveness uses no client ping and no application keepalive (see **Heartbeat** above). Previously, reconnect imposed a 5 s outer deadline on
  the connector despite its 15 s open budget: that cancelled DNS/TCP/TLS/proxy/HTTP
  upgrade work early. The diagnostic is built from `_connect`'s outer
  `asyncio.wait_for` deadline: passing 5 s reproduces exactly
  `TimeoutError: WebSocket open did not complete within 5s`, even when the
  library's `open_timeout` is 15 s. The current recovery path already passes
  15 s; a fresh failure reporting 5 s indicates older running code or an
  overridden deadline, not a credential-refresh timeout. Restart the service
  after deploying the fix rather than relying on an already-running process.
  `_connect` now owns the 15 s open deadline for both initial setup and recovery;
  callers cannot accidentally pass the credential-refresh deadline as its limit.
  A refresh timeout is instead reported as `Timed out refreshing credentials` and
  ends recovery immediately; it is not classified as a timed-out WebSocket open.
  Terminal recovery detail now includes the original drop cause,
  attempt count and recovery gap (redacted and bounded). This does not establish
  which network stage stalled; confirming that needs a real service/network trace.
  No commentary or ambiguous write is replayed.

- **Appends** are serialized per call under one lock, and the call is rechecked for
  ending once the lock is held. If chunk N fails, `live.appended` records only the
  confirmed prefix (`text` is exactly those chunks, `chunks` their count). For the two
  recoverable codes a `live.error` says how many parts were confirmed and whether the
  next one was not sent or may have arrived, and the call stays active. Nothing is retried.
- **Reconnect**: a durable, unthrottled `live.error` warns that a spoken request or an
  interruption may have been missed. A tracked task (at most one at a time, cancelled
  with the call) appends a short speakable notice to the voice model; it is not awaited
  from the reader callback, which would deadlock. No delegation is repeated.
- **Enqueue failure** in the bridge: a speakable notice asks the user to try again and a
  `live.error` is recorded; no `live.delegated` is written. A delegation whose input the
  session tap already consumed leaves no stale queued mapping.
- **Delegation cap** (8 pending): the request is dropped with a `live.error` and at most 4
  rejection notices in flight tell the voice model to ask the user to try again; beyond
  that only the error is recorded.
- **Silence** is normal and never ends the call: no application keepalive, no periodic or
  invented progress appends, no error-count thresholds and no automatic session retries.
  Only the deadline, detach, explicit stop and terminal transport closes end a call. The
  bundled instructions tell the voice model that user silence and quiet agent work are
  normal, never to duplicate a delegation because it is quiet, to keep commentary silent,
  and to explain interruptions honestly, asking for a repeat only if necessary.
  Progress commentary still comes only from real `tool.requested` events.

### Root cause of the ~43 s drop

- **Evidence:** every call in the session database since 2026-10-13 ended at 40 to 49 s,
  whatever the WebSocket ping settings were. A typical end reads `Ended sideband_lost · 00:43
  … sideband WebSocket upgrade rejected (404) … drop: ConnectionClosedError no close frame
  received or sent`. The 404 is the reconnect; by then the call was already gone.
- **Cause:** the `nexus-live` helper used webrtc-rs 0.13 (webrtc-ice 0.13.0). Its keepalive
  sends an ICE consent check (a STUN binding request) only when no packet was sent or received
  for the keepalive interval (2 s). Audio RTP flows every 20 ms and RTCP runs both ways, so
  no consent check was ever sent. The server enforces RFC 7675 consent freshness (about 30 s),
  so the media path ends, the sideband drops without a close frame, and the reconnect is 404.
- **Fix:** `rust/live/Cargo.toml` depends on `webrtc = "0.17.2"` (webrtc-ice 0.17.2). It sends a
  binding request every keepalive interval regardless of media (`last_consent_ping` in
  `agent_internal.rs`). This is the same bug fixed upstream in pion/ice PR #767 and
  webrtc-rs/rtc PR #86. `tests/test_live_packaging.py` pins the locked version. It builds
  with Rust 1.88 and the crate's cargo tests pass.
- **Not verified against a real call.** The expected result is a call that runs past 50 s
  with no `sideband_lost`. Confirm it with a real call's session record before treating this
  as closed.

## Headless CLI (`nexus live`)

`nexus live [--session ID] [--voice V] [--new] [--workspace PATH]` is the same call
without the TUI. Code: `ui/cli/live.py` (`run_live`, `format_event`) over
`ui_support/live_helper.py`; it talks to the daemon only through the host client.

1. Without `--session` it uses the most recent session (by last activity), or creates one.
2. `LiveStatus`: an unavailable status prints the host's message and fix and exits 1.
   The `nexus-live` binary must exist (same lookup as above), else the install hint.
3. Consent: if not recorded, the notice is printed and `Agree and start the call? [y/N]`
   is asked on the terminal. Only `y` calls `LiveConsent(accept=True)`; anything else
   records nothing. If stdin is not a terminal and consent is not recorded, the command
   refuses (exit 1) and never accepts for you.
4. The call starts as in `/live`. Output, one labelled line each: `you: …` and `voice: …`
   (final captions only), `→ agent (queue|steer): …` (delegations), `← voice [commentary|final|notice]: …`
   (appends, text as sent), `error: …`, then `Call ended (<reason>[, N s])`. Events of
   other calls in the same session are skipped.
5. Ctrl+C or Ctrl+D (end of input) hangs up: `LiveStop` with reason `user`, then the helper
   is released. A helper that stops ends the call with its microphone fix (exit 1), so
   does a lost daemon connection. Cancellation during the helper handshake also
   releases a call already accepted by the daemon. The event queue is bounded to
   256 rows with backpressure rather than silently dropping transcripts.
   Shutdown awaits the event stream task before disconnecting.

Typed input during a call is read and ignored (the agent is not steered from this
terminal). Not verified: a real call and microphone from this command.

## Host commands

`LiveStatus`, `LiveStart`, `LiveMute`, `LivePause`, `LiveStop`, `LiveHelperEvent` (see
[host.md](host.md)); results `LiveStatusResult` (also returned by mute and stop),
`LiveStartResult`, `LiveHelperEventResult`. Errors are `ErrorResult(kind, message)`
with a stable `kind`: `live_disabled`, `live_consent_required`, `live_busy`, `live_no_session`,
`live_bad_offer`, `live_bad_voice`, `live_instructions`, `live_auth` (401),
`live_forbidden` (403), `live_rate_limited` (429), `live_signaling`,
`live_no_call_id`, `live_timeout`, `live_not_active`, `live_unavailable`.
`LiveHelperEvent.kind` is one of `ready levels phase dc_error dropped closed`;
`detail` is bounded to 2 KiB. Client methods: `live_status`, `live_start`,
`live_mute`, `live_pause`, `live_stop`, `live_helper_event` in `client/protocol.py`.

**Calling the manager session.** A client opens it with `ManagerOpen`, then `LiveStart(session = <manager id>)`
([manager.md](manager.md#gpt-live)). The daemon refuses that call with `manager_disabled` while `[manager] enabled`
is false, and with `live_consent_required` until `LiveConsent(accept=True, scope="manager")` is recorded: the
manager reads other sessions' titles, status and final reports, and the call carries them to OpenAI, which the base
notice does not cover. The scope is versioned separately (the base consent stays valid for ordinary calls),
recording it records the base too, and revoking the base removes both. For authorising a result read, a delegation
counts only by its request text; the `Voice:` lines of the transcript it carries never do.

**Consent versus approval.** The saved data-sharing consent (base plus manager scope) is the only gate on starting
a call; it is not a per-action approval. Manager launch, send, cancel and show run without a confirmation step, so
the bundled instructions tell the model not to ask for one. The bundled instructions do not mention approvals or permissions at all. The model is told only that when it
is explicitly told the agent has a question it says the question needs attention on screen; answers are given on
screen only. A spoken "yes" only clarifies intent and is never an answer. The bridge may still forward a
`permission.requested` notice (see the event table), but nothing in Live approves on the user's behalf. Permission enforcement is unchanged.

`LiveStatusResult.reason` is `disabled`, `unsupported` (the `websockets`
client is missing from a broken install), `no_credential` (no ChatGPT sign-in; `nexus auth codex login`)
or `busy`.

## Config

See [config.md](config.md): `[live]` `enabled voice max_minutes carry_turns pause_seconds progress
instructions log_raw`, and `NEXUS_LIVE=off`. The WebSocket client (`websockets>=14,<16`) is a
base dependency, imported lazily; the `live` extra (`nexus-harness[live]`) remains only
as a compatibility alias. The audio helper `nexus-live` ships inside published platform
wheels (see [release.md](release.md#second-native-binary-nexus-live)); a source install
without Rust, or without ALSA headers on Linux, has no helper and reports `unsupported`.
The model id is a pinned constant, not config.

In the native terminal these are edited in **Settings → GPT Live** (next to Voice & speech): enable,
voice (`Default (sol)` or one of the nine), longest call, paused-call limit, carried turns (steppers that keep any
valid configured number), spoken progress, an instructions-file path (up to 400 characters in the field), the
developer raw log, and a reset. The pinned model is shown read-only. Values save into `[live]` through the
normal config settings path (no raw TOML) and apply to the next call; the page never starts a call and has no
consent or service management. Code: `ui/ratatui/settings_pages/live.py` over `ui_support/live_settings.py`
(`tests/test_ratatui_settings_live.py`).

**Packaging root cause (0.5.1).** The helper is an `optional` setuptools-rust bin, so
when its Cargo build fails the wheel is built without it and no error is raised. The
locked `uuid 1.28` needs Rust 1.89, while the wheel CI toolchain and the crate's
`rust-version` are 1.88, so the build was skipped on that toolchain. The fix is to hold
`uuid` at `1.18.1` in `rust/live/Cargo.lock` (`cargo +1.88 update uuid --precise 1.18.1`;
a release build then succeeds). A published wheel must contain `nexus-live`: the wheel CI
smoke test (`nexus-live --version`) now treats a missing helper as a hard failure.
Raising the toolchain and `rust-version` together is the alternative, and any locked
dependency must stay within the declared `rust-version`. `tests/test_live_packaging.py`
pins the lock and the toolchain. Not verified: a hosted-CI wheel build with the helper.

## Instructions and continuity

The bundled prompt is written for Nexus (not copied from the references) and is
rendered with the **workspace folder name and the agent name, never the OS user**.
Override with `[live].instructions` or `<workspace>/.agents/live/instructions.md`
(<= 32 KiB, `{{agent}}` and `{{workspace}}` placeholders). The rendered text is the
`instructions` field of `live.started`: what the voice model is told is shown.

Session context: unless `new_conversation`, the instructions get a labelled block of
the current Nexus session (`LiveHost.session_context`, built in `host_support/live.py`
from the shared `nexus.view` projection; `nexus/live` imports no session code). It
holds user prompts and the final assistant text of each turn, oldest first, plus a
status word (running, awaiting input, awaiting permission, error). Internal thinking,
tool calls and tool output are omitted, as are user messages Live delegated itself.
Text is redacted and control characters cleaned (pattern based: not a guarantee), at
most 60 items of 2,000 characters, quoted line by line as untrusted background that
is never instructions. The block is clipped to 8 KiB keeping the newest items, and
says how many older messages were omitted. `live.started` records
`session_context_items` and `session_context_omitted`. A projection failure or a
deleted session yields no block and the call still starts.

Continuity: one voice conversation per Nexus session. The last `carry_turns` (12)
final transcript turns of earlier calls, trimmed to 8 KiB, are appended after the
session context as quoted background and counted in `live.started.carried_turns`.
The rendered instructions as a whole stay under 32 KiB (the carry-over gets what the
base prompt and session context leave). `new_conversation` starts empty (neither
block), and later calls then carry only turns after it. What the real service does
with instructions of this size is not verified.

## Events and view

Durable, turn-less, one `call_id` per call (see [events-and-view.md](events-and-view.md)):

| Event | Data |
| --- | --- |
| `live.started` | `model voice instructions_source instructions carried_turns new_conversation session_context_items session_context_omitted` |
| `live.transcript` | final only: `role text turn truncated` |
| `live.transcript.delta` | the current partial caption `role text`; coalesced to <= 4 Hz |
| `live.delegated` | `delegation_id text mode queued_id source heard verbatim` |
| `live.appended` | `delegation_id channel(commentary/final/notice) text chunks`, text exactly as sent |
| `live.phase` | `connecting listening speaking working muted paused`; <= 4 Hz |
| `live.error` | a redacted non-fatal error, <= 1 per second (reconnect, partial-append and enqueue warnings are always recorded) |
| `live.levels` | reserved, never persisted (see decisions) |
| `live.ended` | `reason duration_s error` (reason includes `pause_timeout`) (redacted), exactly one per `live.started` |

The reducer folds them into `ConversationView.live_calls` (`LiveCallView`:
transcript, delegations, appends, caption, phase, errors; bounded). Replay of the
log reproduces the live state (`tests/test_live_view.py`).

## Testing

`tests/test_live_protocol.py`, `test_live_transport.py` (httpx `MockTransport`,
fake socket), `test_live_manager.py`, `test_live_view.py`, `test_live_bridge.py`
(a real `Runtime` with `ScriptedProvider`), `test_live_config.py`,
`test_live_layering.py`, the Live cases in `test_host_facade.py`, and
`test_live_security.py` (no bearer token or account id reaches events, results,
the view, the raw dev log or caplog). Fakes live in
`tests/live_fakes.py`. No test touches the network.

## Not verified / deferred

- Everything against the real service: signaling headers (the `originator` is the
  one `CodexOAuthManager` already sends, `nexus`), whether attestation is ever
  required, the default channel of an append without one, transcript event
  semantics (cumulative vs delta; both are handled), ICE/trickle behaviour,
  voice-name validation.
- Real microphone and speaker use, a real call from the terminal client (helper
  spawn, mic permission prompts, echo behavior), the call bar on a real terminal,
  and the consent record across a real daemon restart: only fake-helper tests exist.
- Spoken "stop" and AEC (optional M6).
- A live smoke test (`-m live`) is not written.
