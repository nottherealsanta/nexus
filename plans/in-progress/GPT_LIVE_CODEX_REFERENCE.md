# GPT Live: what OpenAI's Codex CLI does (reference notes)

Source: `openai/codex`, cloned at `/Users/santa/repos/codex`, commit
`6ae8b8aa7c608730d4315d59c5c081897ae02780` (2026-10-16, shallow). Licence: Apache License 2.0
(`LICENSE`, `NOTICE`: "Copyright 2025 OpenAI"). These are reference notes written from
reading the source. No code was copied, nothing was built or run, and quotes are kept to short
rule fragments. Paths are relative to `codex-rs/` unless they start with `nexus/` or `rust/`.
"UNCONFIRMED" means the source does not settle it. Nothing here was checked against the live
service. Codex calls this protocol "Frameless Bidi", "realtime v3" or `quicksilver=v2`, with model
`gpt-live-1-codex`.

Compared against: `docs/live.md`, `plans/in-progress/GPT_LIVE_PROTOCOL_NOTES.md` (call it **PN**),
`nexus/live/{protocol,transport,manager,bridge}.py`, `nexus/live/prompts/instructions.md`,
`rust/live/src/{peer,app}.rs`.

Citation key: `core/...` = `codex-rs/core/src/...`; `api/...` = `codex-rs/codex-api/src/endpoint/...`.

---

## Summary

1. Codex confirms the Nexus call shape: signaling POST that returns the SDP answer and a call id
   in `Location`, then a join of `wss://.../v1/live/<call id>`. It has two request shapes
   (ChatGPT backend JSON; API-key multipart). Nexus matches the ChatGPT one.
2. **Codex has no mechanism for the voice model to author its own message to the agent.**
   `delegation.created` is parsed for `item.content[].input_text` and that text is the "input".
   Codex then adds a second field, `<transcript_delta>`, built client-side from the transcript
   events (both speakers, since the last handoff). Codex's own voice prompt says the opposite of
   Nexus's: "Do not paraphrase the request". No function tools are declared on this protocol.
3. The only model-authored handoff in Codex is the *different* `RealtimeV2` protocol
   (`gpt-realtime-1.5`, plain Realtime API), which declares a `background_agent(prompt)` function
   tool. Even there the tool description tells the model to pass the user's own words.
4. On connection lifetime: Codex never sends client-initiated WebSocket pings (it only answers
   server pings), never sends anything on the sideband at join, and treats an unclean sideband
   loss as recoverable with unbounded reconnects (200 ms to 5 s backoff) until the server says
   404 or 410. Nexus sends pings every 20 s and gives up after 2 reconnects. See section 2 for
   the ranked hypotheses.

---

## 1. Call setup

### 1.1 Which transport the TUI uses

WebRTC for audio plus a server-side WebSocket sideband for control, both. There is no audio over
the WebSocket for this model (the WebSocket carries control and transcripts only).

- TUI asks app-server to start a realtime session with `transport = Webrtc{sdp}`,
  `version = V3` and `client_managed_handoffs = true`, `include_startup_context = false`,
  `prompt = None` (`tui/src/app_server_session/realtime.rs:34-58`). The offer SDP is produced
  locally by a separate helper process (`codex-voice-host`, `voice-host/`), driven from
  `tui/src/chatwidget/realtime.rs:295-345`.
- Core makes the signaling POST (`core/src/client.rs:740-775`), keeps the call id and auth headers
  server-side, and joins the sideband itself (`core/src/realtime_conversation/sideband.rs`).
  The SDP answer goes back to the client as an event (`core/src/realtime_conversation.rs`
  `RealtimeConversationSdp`, around 1740).
- The data channel `oai-events` is created by the client but its messages are discarded
  (`voice-host/src/transport.rs:113-136`, `OnMessage(_) => {}`); "typed events use the existing
  sideband" (`voice-host/src/transport.rs:2`). Only its open state is used to signal readiness.
- A plain WebSocket start (`ConversationStartTransport::Websocket`) also exists, using
  `input_audio.append` frames (`api/realtime_websocket/methods.rs:383-392`). That is the
  non-WebRTC "frameless" audio path; the TUI voice path does not use it.
- `voice-host/README.md` says it "does not enable voice in the TUI". The TUI code does use it
  (`/voice` slash command, `tui/src/slash_command.rs:142`), so the README is stale or voice is
  gated by packaging. UNCONFIRMED which.

### 1.2 URL, method, body

Two request shapes, chosen by whether the provider base URL contains `/backend-api`
(`api/realtime_call.rs:66-69`).

| | ChatGPT backend (Nexus uses this) | API-key provider |
| --- | --- | --- |
| Path | `.../backend-api/codex/realtime/calls` (`realtime_call.rs:55-57`) | `<base>/live` for Frameless (`realtime_call.rs:66-75`) |
| Query | `?intent=quicksilver&architecture=avas` (`realtime_call.rs:213-223`) | none for Frameless; the same two params for the legacy v1 parser |
| Body | JSON `{"sdp": ..., "session": {...}}` (`realtime_call.rs:147-166`) | `multipart/form-data`, boundary `codex-realtime-call-boundary`, parts `sdp` (`application/sdp`) and `session` (`application/json`) (`realtime_call.rs:168-205`) |
| Response | raw SDP answer body; call id from `Location` | same |

Test evidence: `core/tests/suite/realtime_conversation.rs:815-1000` asserts path
`/backend-api/codex/realtime/calls`, query `intent=quicksilver&architecture=avas`, and body
`sdp`, `session.delegation.type == "client"`, `session.model`. The API-key mock answers
`POST /v1/live` with `Location: /v1/live/rtc_...` (`.../realtime_conversation.rs:1222-1240`).

Call id: last path segment of `Location` (query stripped) that is `rtc_<non-empty>` or a canonical
36-char UUID (`api/realtime_call.rs:259-294`). Codex has **no** `openai-session-id` fallback.
The sideband URL is the WebSocket base joined with `/v1/live/<call id>`
(`api/realtime_websocket/methods.rs:1380-1415`, `normalize_realtime_path` at 1417); the base
defaults to `https://api.openai.com/v1` (`methods.rs:63`).

### 1.3 Headers

From `core/src/realtime_conversation.rs:1954-1995` (`realtime_request_headers`) plus
`:1480-1540` (session headers) and `login/src/auth/default_client.rs:456-470`:

| Header | Value |
| --- | --- |
| `openai-alpha` | `quicksilver=v2` for Frameless (`v1` for the legacy parser) (`realtime_conversation.rs:1964-1971`) |
| `x-session-id` | the *realtime* session id: caller-supplied or the thread id (`:1976-1981`, `:1622`). Test asserts it differs from `session-id`/`thread-id` (`tests/suite/realtime_conversation.rs:927-940`) |
| `session-id`, `thread-id` | Codex session/thread ids (`build_session_headers`, `realtime_conversation.rs:1450-1460`) |
| `voice-session-id` | optional, caller-supplied (`:1983-1987`) |
| `originator` | `codex_cli_rs` by default (`default_client.rs:42`), overridable per thread (`add_originator_header`) |
| `User-Agent` | `<originator>/<version> (<os> <ver>; <arch>) <terminal>` (`default_client.rs:152-178`) |
| `x-codex-turn-metadata` | `{"thread_source": ...}` only, when set (`realtime_conversation.rs:1462-1472`) |
| `Authorization` | provider auth: ChatGPT bearer plus account id, or API key (`core/src/client.rs:700-735`) |
| `x-oai-attestation` | only when the model provider supports it and an `AttestationProvider` was injected (`core/src/client.rs:752-755`, `:903-915`; provider default is `false`, `model-provider/src/provider.rs:150`). The only provider in-tree is app-server's, i.e. the desktop host (`app-server/src/attestation.rs`). The CLI/TUI does not generate one by itself. |

Same headers, plus attestation, go on the sideband handshake (`core/src/client.rs:776-790`).

So: attestation is optional in Codex too. Nexus's identity choice (own `originator`) is a decision,
not a requirement visible in Codex. Whether the server enforces it by plan or region is UNCONFIRMED.

### 1.4 Session body (Frameless)

`api/realtime_websocket/methods_frameless_bidi.rs:52-102` (`session_json`):

```
{ "model": "gpt-live-1-codex",            // default, core/src/realtime_conversation.rs:113,1589-1600
  "instructions": "<text>",
  "audio": { "output": { "voice": "<name>" } },
  "delegation": { "type": "client", "ack_filler": <bool, optional> },
  "initial_items": [ {type:"message", role, content:[{type: input_text|output_text, text}]} ]  // optional
}
```

- The `id` key is removed before sending (`realtime_call.rs:144`).
- **Not sent**: tools, tool_choice, turn detection, input/output audio format, transcription
  settings. Those exist only on the RealtimeV2 shape (`methods_v2.rs:48-120`: PCM 24 kHz,
  `server_vad` with `silence_duration_ms: 500`, `gpt-4o-mini-transcribe`). Server defaults apply
  to Frameless. Matches PN section 1.3.
- `delegation.ack_filler`: "Whether a realtime V3 delegation produces an acknowledgement filler.
  `None` preserves the Realtime API's default behavior" (`protocol/src/protocol.rs:183-185`).
  Codex never sets it in the TUI. Effect when set is UNCONFIRMED (no test observes audio).
- `initial_items` is a real session field (up to 128 items, 8,192 estimated tokens in total,
  `realtime_conversation.rs:100-107`, `:1546-1580`), accepted only for v3. The TUI does not use it
  (`initial_items: None`). Nexus instead appends prior turns into `instructions`.
- Voices for v3: nine legacy names (the same nine as PN section 7) plus sixteen more (`aube`,
  `bubbie`, `haetsal`, `hanul`, `himari`, `leher`, `miko`, `neer`, `porto`, `rindo`, `rio`,
  `rivage`, `selva`, `sonna`, `tinta`, `viola`); default `cove` (`protocol/src/protocol.rs:354-407`).
  Codex validates the voice client-side before calling (`realtime_conversation.rs:1670-1695`).

### 1.5 Offer/peer details (from `voice-host`)

- Non-trickle offer: waits for ICE gathering to complete before reading the local description
  (`voice-host/src/transport.rs:149-169`, 15 s limit).
- One audio track, Opus PT 111, 48 kHz, declared 2 channels, `minptime=10;useinbandfec=1`;
  encoder is mono, `Application::Voip`, 20 ms frames (`voice-host/src/audio_track.rs:19-50`, `:85-110`).
- Data channel `oai-events`, default options (`transport.rs:113`).
- Answer handling: Codex parses the answer SDP itself, takes at most 32 unique component-1
  candidates, and passes each one to `add_ice_candidate` explicitly "to start TCP dialing"
  (`transport.rs:171-216`). It listens on UDP and TCP (`with_tcp_addrs`, `transport.rs:100-101`)
  and raises ICE check attempts to 200 ms x 75 ("Upstream defaults exhaust checks after 1.4s",
  `transport.rs:86-92`). It uses webrtc/rtc `0.20.3` (`voice-host/Cargo.toml`); Nexus uses
  webrtc-rs `0.17.2`.
- Muted or idle capture still sends Opus silence packets: "Muted capture sends generated silence to
  keep the peer alive" (`audio_track.rs:1`, `capture_worker.rs:3,117`). Elapsed time advances in
  whole 20 ms packets so the RTP clock stays on the receiver's grid (`audio_track.rs:117-160`).
  Nexus does the same for mute and gating (`rust/live/src/app.rs:112-118`) but sends a fixed
  20 ms per sample (`rust/live/src/peer.rs:154-159`) with no wall-clock gap handling.

---

## 2. Sideband and lifetime

### 2.1 What Codex does

- URL, headers: section 1.2, 1.3. Handshake via tungstenite defaults (`WebSocketConfig::default()`,
  `methods.rs:1324`), no subprotocol.
- **Nothing is sent at join** for Frameless WebRTC. `session.update` is skipped for
  `FramelessBidi` on the WebRTC sideband and for existing-call joins
  (`methods.rs:1239-1262`). For a standalone WebSocket start, `session.update` is sent and the
  client then waits for `session.started` (`methods.rs:1250-1262`, `:655-672`).
- **No client pings.** The only ping handling is replying `Pong` to a server `Ping`
  (`methods.rs:136-145`), and `Ping`/`Pong` are ignored in the read loop (`methods.rs:650`).
  `grep` for `ping_interval`, `keepalive`, `heartbeat` in `codex-api` finds nothing client-driven.
- Join retries: `provider.retry.max_attempts` with `backoff(base_delay, n)`, except 404/410 which
  are final and policy denials which are final (`methods.rs:1090-1135`). For a client-created
  call it also retries 404 at 100, 300 and 800 ms, because "Instant Connect returns a call ID
  before ICE activates the call. Its sideband can briefly return 404 while the client connects
  media in parallel" (`methods.rs:1058-1088`).
- **Reconnect after an unclean loss** (Frameless only): loop forever while the call is active;
  delay `200 ms * 2^(n-1)` capped at 5 s, where `n` counts rapid disconnects; the counter resets
  when a connection lasted at least 30 s (`sideband.rs:23-25`, `:130-165`, `:185-190`). While
  reconnecting, a 404 or 410 on the rejoin means the call ended and it stops quietly
  (`sideband.rs:96-103`, `:170-176`). Pending outbound messages are kept and replayed after
  reconnect (`pending_outbound`, `sideband.rs:56,150`). The transcript state survives the reconnect
  (`sideband.rs:62-70`).
- Test: `conversation_webrtc_live_reconnects_sideband_after_unclean_disconnect`
  (`tests/suite/realtime_conversation.rs:1222`) drops the socket, expects the same
  `GET /v1/live/rtc_reconnect` handshake, delivers a `delegation.created` after the reconnect, then
  answers the next join with `410 Gone` and expects exactly one more attempt and a clean close
  reason `transport_closed` (`:1385-1407`, `:1500-1515`). It also floods inbound traffic to prove
  outbound sends are not starved (`:1277-1340`).
- Close handling (`methods.rs:623-650`): a `Close` frame with code `Normal` is end of stream;
  any other code, no code, or an ended stream is `ApiError::Stream("... closed unexpectedly")`,
  which triggers the reconnect loop for Frameless. Binary frames become a `RealtimeEvent::Error`.
  Client close: sends `{"type":"session.close"}` then a WebSocket close (`methods.rs:478-503`).
- Errors: an `error` event is surfaced as `RealtimeEvent::Error` (message from top-level
  `message`, else `error.message`, else stringified `error`, `protocol_common.rs:63-83`) and the
  fan-out ends the conversation with `end = Error` (`core/src/realtime_conversation.rs:1798-1802`,
  `:2720-2722`). So Codex treats every `error` event as terminal (as OMP does, per PN section 3.4).

### 2.2 Anything that explains a call dying ~35-40 s after connect?

Codex has no timer, heartbeat or `session.update` of that kind. Nothing in the source says "the
server needs X every N seconds". The differences that could matter, ranked by my confidence:

1. **Client-initiated WebSocket pings.** Nexus configures `ping_interval=20` in
   `nexus/live/transport.py:177-201`. Codex never sends one. If the sideband server or an
   intermediary does not answer pings, the `websockets` keepalive closes the socket at about
   `interval + timeout`. Nexus already changed the timeout to 60 s to test this. Caveat: with
   `websockets`, a keepalive timeout normally *sends* close code 1011 ("keepalive ping timeout"),
   but the recorded drop reads "no close frame received or sent" (`transport.py:177-181`), which
   fits an abrupt TCP loss better. Do not treat this as proven. Cheap test: `ping_interval=None`.
2. **The media leg, not the sideband.** The server runs ICE and the call; if the browserless
   peer stops answering STUN consent or RTP, the server may end the call and close the
   sideband around 30 s later (RFC 7675 consent freshness is 30 s; this is general knowledge, not
   from Codex). Codex's peer differs from Nexus's: explicit remote candidates plus TCP candidates,
   200 ms ICE checks for 15 s, wall-clock-aligned RTP, newer webrtc crate. Nexus uses
   `RTCConfiguration::default()` and webrtc-rs 0.17.2 (`rust/live/src/peer.rs:101-103`). If the
   dying call has `peer_failed`/`peer connection state` lines in the helper log, this is the lead.
3. **Bounded reconnect.** Even if (1) is only a transport blip, Nexus gives up after
   `MAX_RECONNECTS = 2` and never resets it (`transport.py:69-71`, `:478-506`), whereas Codex
   retries until the server says 404/410. A brief sideband loss ends a Nexus call that Codex would
   have kept.
4. **Join timing.** Codex joins the sideband right after the POST returns, in parallel with ICE,
   and tolerates early 404 (`methods.rs:1058-1088`). Nexus's retry (200 * 2^n, 5 attempts) is
   similar, but it does not treat 404 specially: check that a 404 on early join is retried and a
   404/410 on rejoin is terminal.
5. **Ruled out as causes** (Codex does not do them either): attestation, `session.update` at join,
   periodic empty appends, `response.create`, any heartbeat message.

Not verified: all of the above is source reading; no call was made.

### 2.3 Events

Inbound (`api/realtime_websocket/protocol_frameless_bidi.rs:15-33`): `session.started`,
`session.updated` (both read `session.id`, optional `session.instructions`), `output_audio.delta`
(field `audio`; 24 kHz mono default), `input_transcript.added`, `output_transcript.added`
(`item.text`), `turn.done` (`turn.role`, `turn.transcript`), `delegation.created`, `error`.
Everything else is logged at debug and dropped. Codex does **not** read
`session.*_transcript.delta`, `session.closed`, `output_audio_buffer.cleared`.

Transcript semantics, which resolves PN open question 4: Codex treats `*_transcript.added` as
**deltas to append** and `turn.done` as the **final text that replaces** the accumulated entry
(`methods.rs:877-964`). The two speakers are interleaved, with a delayed final ignored if it does
not extend what was accumulated (`methods.rs:925-942`). The events may also carry `start_ms` /
`end_ms`, which Codex uses to group both speakers of one frame (`methods.rs:306-310`,
`:673-695`). Nexus's own capture saw cumulative text in `live.transcript.delta`; reconcile before
relying on either.

Outbound (`api/realtime_websocket/protocol.rs:62-95`): `delegation.context.append`
(`delegation_item_id`, optional `channel`, `content:[{type:"input_text",text}]`),
`session.context.append` (same without the id), `session.close`, `input_audio.append`
(WebSocket-audio path only). Appends are chunked at 500 UTF-8 bytes on a char boundary
(`methods_frameless_bidi.rs:11,104-123`, same as PN section 4.4). A comment states "An omitted
channel defaults to speakable on this wire protocol" (`core/src/realtime_conversation.rs:2431`),
which resolves PN open question 2.

---

## 3. Handoff / delegation

### 3.1 Who writes the text the agent receives

**The voice model does not write a separate message in Codex's Frameless integration.** The chain:

1. Server sends `delegation.created` with `item.content[]` of `input_text`. Codex concatenates
   those parts with an empty separator (`protocol_frameless_bidi.rs:73-98`; PN says OC joins with
   `""` and OMP with `"\n"`; Codex matches OC). It builds `RealtimeHandoffRequested{handoff_id,
   item_id, input_transcript, active_transcript}`. A unit test asserts the legacy
   `conversation.handoff.requested` and the Frameless `delegation.created` decode to the same
   handoff, with `input_transcript: "check the weather"` taken from the content text
   (`protocol_frameless_bidi_tests.rs:6-35`).
2. The connection layer fills `active_transcript` from its own transcript tracking: the entries
   accumulated since the previous handoff (both speakers), with the handoff input appended as a
   user entry if not already present (`methods.rs:760-775`, `append_handoff_input` at 964).
3. Core renders the text for the agent (`core/src/realtime_conversation.rs:1892-1926`,
   `core/src/context/realtime_delegation.rs:51-69`):
   ```
   <realtime_delegation>
     <input>{delegation content, XML-escaped, max 4 KiB, keep start}</input>
     <transcript_delta>{"user: ..." / "assistant: ..." lines, escaped, max 4 KiB, keep end}</transcript_delta>
   </realtime_delegation>
   ```
   (`<source>transcript_tail_flush</source>` is added for a session-end flush.) If the delegation
   content is empty, the transcript delta is used as the input (`:1905-1909`). The wrapper is a
   user-role contextual fragment (`realtime_delegation.rs:36-38`).
4. That string is submitted as a normal user turn with trigger `"realtime"`, using
   `StartOrSteer`, so it starts a turn when idle and steers the running one otherwise
   (`core/src/session/turn_input.rs:730-748`). The agent also gets a developer message when the
   call starts telling it the input "may be unpunctuated or contain recognition errors"
   (`prompts/templates/realtime/realtime_start.md`, injected by
   `core/src/context/realtime_start_instructions.rs`).

So Codex gives the agent **both** whatever the server put in the delegation content *and* the
recent spoken transcript, labelled. It never relies on the voice model's wording alone, and it
does not rewrite the user's words either. Whether the server-side delegation content is the heard
utterance or something the voice model wrote is UNCONFIRMED from this repo: all fixtures are
hand-written (`"check the weather"`, `"explain the marker"`, `"hello world"`). The prompt it gives
the voice model asks for pass-through (section 5), which is consistent with the Nexus
observation in PN (the delegated text equalled the final user transcript, noise included).

### 3.2 Reply path (agent to voice)

Codex has two modes (`client_managed_handoffs`). The TUI runs the **client-managed** mode
(`tui/src/app_server_session/realtime.rs:34`), in which core does not mirror agent output
automatically (`core/src/realtime_conversation.rs:955-957`, `:1207`) and the TUI decides what is spoken.

TUI client-managed:
- Agent turns that came from voice are tracked as "delegated" and only the final answer may be
  spoken. Commentary and reasoning stay hidden from the UI
  (`tui/src/chatwidget/realtime.rs:660-720`, `tui/src/app/tests/realtime_handoff_e2e.rs:26-250`).
- The final text is sent as `thread/realtime/appendSpeech` (`Op::RealtimeConversationSpeech`),
  which core sends as a **`session.context.append` with `channel: "speakable"`**, not a
  `delegation.context.append` (`core/src/realtime_conversation.rs:2014-2029`, `:1171-1194`,
  `:2436-2442`). The e2e test asserts exactly this body:
  `{"type":"session.context.append","content":[{"type":"input_text","text":"[ANALYSIS] ..."}],"channel":"speakable"}`
  (`tui/src/app/tests/realtime_handoff_e2e.rs:195-207`). No `delegation_item_id` is attached.
- Speech text is capped at 990 estimated tokens (`MAX_SPEAKABLE_FINAL_TOKENS`,
  `tui/src/chatwidget/realtime.rs:73`; core limit 1,000, `realtime_conversation.rs:103`).

Core-managed (non-TUI default, `client_managed_handoffs=false`):
- Final and partial agent messages go back as `delegation.context.append` with the active
  delegation id (`handle_handoff_output`, `realtime_conversation.rs:2374-2480`, Frameless arm
  at 2420-2490). The channel comes from `codex_response_handoff_mode`: `Thinking` sends no channel
  (so speakable), `Commentary` sends `commentary`, `BemTags` picks per message phase
  (`v3_output_writer`, `:2352-2371`).
- Reasoning summaries are sent as `delegation.context.append` on the `commentary` channel,
  quiet, only while that handoff is still active (`QuietReasoningStatus`, `:2426-2435`,
  `send_reasoning_status` at 911, 256-byte cap `:104`).
- There is **no** "Agent Final Message" label in Frameless; the label constant
  (`AGENT_FINAL_MESSAGE_PREFIX`, `realtime_conversation.rs:112`) is used only for the legacy v1
  parser (`:1021-1030`, `methods_common.rs:15,86-90`). RealtimeV2 prefixes `[BACKEND] `
  (`:114-115`). Nexus uses the label on a Frameless call (`nexus/live/prompts/instructions.md`);
  it may still work (the model follows its prompt) but Codex does not rely on it.
- Approvals/questions are spoken as a fixed sentence ("I need your approval to continue. Please
  review the request in the app.", `core/src/session/turn.rs:2192-2199`); the full event JSON is
  *not* sent as speech in the TUI path (client-managed).

### 3.3 End of call

If untranscribed speech remains when the call ends, Codex routes it to the agent as a final
handoff tagged `transcript_tail_flush` with an instruction that "You probably do not have to do
anything" (`core/src/realtime_conversation.rs:122`, `:2283-2300`, `transcript_tail.rs`), and writes the
tail into history before closing (`:1826-1840`).

### 3.4 What the e2e tests assert

`tui/src/app/tests/realtime_handoff_e2e.rs` (one test) drives a mocked `delegation.created`
(`"explain the marker"`) through real core and app-server and asserts:
1. The agent's final answer, marked `final_answer`, is spoken exactly once, as the
   `session.context.append`/`speakable` body above.
2. Commentary and reasoning (`PRIVATE COMMENTARY`, `PRIVATE REASONING`) never appear in rendered
   history or in the visible buffer.
3. The call survives switching the visible thread (voice owner stays the original thread).
4. Replayed live captions are not duplicated (one line count).
5. A typed follow-up on the same thread renders normally and is **not** spoken.

`core/tests/suite/realtime_conversation.rs` adds: the call POST headers/body (815-1000); WebRTC
sideband join with `call_id` and no `session.update`; reconnect with the same call id and a 410
terminal (1222-1530); input not starved by inbound flood. `realtime_misalignment.rs` checks that a
queued voice handoff is retired and does not start a turn after a safety stop (an interesting
guard Nexus does not have: `RealtimeHandoffAdmission`, `realtime_conversation.rs:136-168`).
`app-server/tests/suite/v2/realtime_conversation.rs` covers the app-server surface, including
`delegation_ack_filler` plumbing and a V2 `background_agent` tool call (`:666-678`).

---

## 4. Tools

- **Frameless / `gpt-live-1-codex`: no function tools are declared** (`session_json` has no
  `tools`; section 1.4). The only way work reaches the agent is the `delegation` item.
- **RealtimeV2** (`gpt-realtime-1.5`, `api/.../methods_v2.rs:21-120`) declares two tools with
  `tool_choice: "auto"`:
  - `background_agent` with `{prompt: string}` (required, `additionalProperties:false`).
    Description (key rules): use as the default action; "Do not rephrase the user's ask or rewrite
    it in your own words; pass along the user's own words"; if the agent is busy, the call steers
    the running task; "do something next / later" must also call it (`methods_v2.rs:34`).
  - `remain_silent` with `{}`: call it when the best response is to say nothing, after hidden
    control messages or background updates (`methods_v2.rs:35-36`).
  Flow: `conversation.item.create` of type `function_call_output` with the `call_id`
  (`methods_v2.rs:63-76`); a steer is acknowledged with a fixed string and `response.create`
  (`core/src/realtime_conversation.rs:2667-2705`, strings at 116-119). This is the only place Codex
  has a model-authored `prompt`, and it is a different protocol and model.
  RealtimeV2 is rejected for AVAS/WebRTC calls (`realtime_call.rs:230-238`).

---

## 5. Instructions and context

### 5.1 Voice-model instructions (session `instructions`)

Default text: `prompts/templates/realtime/backend_prompt.md` (rendered with the OS user's first
name, `core/src/realtime_prompt.rs:5-30`; an operator override replaces it entirely,
`:6-12`). Short rule fragments:

- Persona: "You are Codex ... Be concise, clear, and efficient"; "playful collaborator".
- One assistant: "Do not mention anything about backend"; "Present every work as done by you".
- "Pass execution work to the backend"; "NEVER refuse requests. Delegate all user requests to the
  backend"; "For any actions/tasks, always use the backend. If it is unclear ... use it."
- Steering: "Running backend work remains steerable ... immediately delegate"; "Do not claim that a
  running backend task cannot be updated".
- Message markers: user input is prefixed `[USER] `, backend `[BACKEND] ` (matters for V2).
- Presenting results: key takeaway only; "Do not read out or recreate tables, diffs ... code
  blocks"; the user "can see the full interaction with the backend".
- Preferences persist: update frequency / verbosity set by the user is "active task-level".
- Style: "When the user makes a clear request, proceed directly. **Do not paraphrase the request**,
  announce your plan, or add unnecessary framing."

Compare Nexus's prompt: rewrite every project request "in your own words", never pass the
transcript through. Codex deliberately does the reverse (and Nexus's observed data show the
model does not do the rewrite anyway; see PN, "Authorship of delegation text").

### 5.2 Agent-side instructions (developer message when the call starts/ends)

`realtime_start.md`: the agent is "a backend executor behind an intermediary"; "Any response you
produce will be consumed by the intermediary and may be summarized"; routed user text "may be
unpunctuated or contain recognition errors"; keep responses "concise and action-oriented"
(`core/src/context/realtime_start_instructions.rs`, size cap 8,192 tokens,
`realtime_conversation.rs:106,1527-1544`). `realtime_end.md`: typed text resumes; "Do not assume
recognition errors". Nexus has no equivalent agent-side notice.

### 5.3 Session context

- TUI: no startup context (`include_startup_context: Some(false)`), no `initial_items`; the voice
  model starts with the prompt only.
- Other clients can ask for startup context built by
  `core/src/realtime_context.rs` (budgeted sections: current thread 1,200 tokens, recent work
  2,200, workspace tree 1,600, notes 300; wrapped in `<startup_context>` with "may be incomplete
  or stale ... do not repeat it back"), appended to the instructions
  (`realtime_conversation.rs:1546-1566`), and/or `initial_items` for v3.
- During the call, context reaches the voice model only through appends: agent output (above),
  typed text via `session.context.append` (`conversation_item_create_message` Frameless arm,
  `methods_common.rs:36-48`).

---

## 6. UI (TUI)

- Voice is toggled with `/voice` (`tui/src/slash_command.rs:142`); there are keymap actions
  `toggle_voice` and `toggle_voice_mute` (`tui/src/keymap.rs:136-138`).
- A one-or-two-row **voice strip** sits in the bottom pane: `voice <marker> <activity>`, mute /
  unmute control with a shortcut hint, and `mic` / `codex` level meters drawn from sample
  histories; a "connecting" vs "active" phase; reduced-motion hides the meter row
  (`tui/src/bottom_pane/voice_strip.rs:1-110`).
- Captions: both speakers' transcripts are inserted into the normal scrollback as history cells
  with a bounded **split-flap animation** (`tui/src/chatwidget/realtime_split_flap.rs`), partials
  bounded to 1 KiB each and 32 pending cells (`chatwidget/realtime.rs:51-56`). Settled text never
  re-animates; widget replacement keeps captions.
- The delegated agent's turns appear as ordinary turns in the thread; reasoning and commentary are
  suppressed for voice-originated turns (section 3.4); a typed turn on the same thread stays
  visible and silent.
- One voice owner at a time across threads (`tui/src/app/voice_owner.rs`); voice keeps running
  while another thread is on screen, and a banner/agents overview reflects it.
- Speaker interruption and recovery states exist (`INTERRUPTION_ACKNOWLEDGMENT` 400 ms,
  `SPEAKER_ACTIVITY_HOLD` 500 ms, `chatwidget/realtime.rs:61-66`).

---

## 7. Differences, ranked by impact

### 7a. Voice model's own message to the agent

Codex offers no such channel on `gpt-live-1-codex`: no tool, no field. Nexus's rewrite rule
cannot work if the server places the heard utterance in `item.content`, and the only evidence
available (PN, `live.delegated` vs transcript) says it does. Ranked options:

1. **Add the recent transcript to what the agent gets, labelled, and tell the agent it is speech.**
   Codex's `<realtime_delegation><input>...</input><transcript_delta>...</transcript_delta>`
   and its agent-side developer note. This gives the agent the context the user actually spoke
   (including the voice model's replies and earlier fragments) with no dependence on the voice
   model's wording. In Nexus the bridge already records `heard`; extend it to include both roles
   since the last delegation, bounded (Codex: 4 KiB each, keep the start of input and the end of
   the delta).
2. **Make the agent do the rewrite.** Since the voice model will not, tell the agent via a
   developer-style notice that the text is a speech transcript with recognition errors and that it
   should interpret intent, and keep the original text visible to the user. This matches Codex's
   `realtime_start.md` and is the only reliable "rewrite" available.
3. **Stop instructing a rewrite the model cannot perform**, or keep it only as a harmless request:
   it costs prompt tokens and, per Nexus's data, did not change the output. If kept, say so in
   the docs. (Codex's own prompt asks for pass-through.)
4. **Test whether the server honors the model-written content at all** with a controlled spike
   (log raw frames, `[live].log_raw`): ask for a hand-off with a deliberately different wording
   and compare `item.content` with `turn.done`. Also try `delegation.ack_filler` true/false to see
   whether it changes the audio before the delegation text. Neither is answered by Codex.
5. A *tool* is not an option on this protocol (Codex declares none). Declaring function tools in
   the session body for `gpt-live-1-codex` is untested by anyone I have found; try only in the
   spike and expect a rejection. The V2 `background_agent(prompt)` pattern needs the different
   `gpt-realtime-1.5` model, a PCM/`server_vad` session and no AVAS/WebRTC.

### 7b. Connection lifetime

1. Remove the client-initiated sideband pings (`ping_interval=None`), as Codex does; keep answering
   server pings (the library does it). Highest-value single experiment (section 2.2 item 1).
2. Make sideband loss recoverable like Codex: unlimited reconnects with `200 ms * 2^n`, cap 5 s,
   reset the counter after 30 s of stability; stop only on 404/410 or `LiveStop`; keep the
   undelivered outbound message and replay it after reconnect (Nexus treats ambiguous writes as
   never replayed, which is safer for delegation replies but loses them; Codex replays at most one
   pending message). Transcript state must survive the reconnect.
3. Verify ICE and RTP robustness against Codex's peer: explicit remote candidates (including TCP),
   200 ms check interval for 15 s, wall-clock aligned RTP timestamps and silence while muted,
   upgrade to the newer webrtc crate only if (1) and (2) do not fix it. Log ICE/consent/peer
   state transitions with timestamps to see which side dies first at ~35 s.
4. Treat early 404 on the join as "not active yet" (100/300/800 ms), and 404/410 on rejoin as
   "call ended" (clean close, not an error).
5. Do not send anything at join (Nexus already complies).

### 7c. Other differences

| # | Nexus | Codex | Impact |
| --- | --- | --- | --- |
| 1 | Prior turns and session history appended into `instructions` (`manager.py:410-434`) | `initial_items` (v3 message items; 128 items, 8,192 tokens) and optional startup context | Medium. `initial_items` keeps instructions short and gives true role-tagged history. Untested against the live service; try it in the spike. |
| 2 | Agent reply sent as `delegation.context.append` with the "Agent Final Message" label (bridge) | TUI: final answer as `session.context.append` `speakable` with no id; core-managed: `delegation.context.append` with no label; progress as quiet `commentary` | Medium. Labels are a v1 artefact. Consider dropping the label and relying on the `speakable`/`commentary` channels. Keep the delegation id for progress. |
| 3 | Reply capped at 1,200 chars (OC value) | Final speech capped at about 1,000 tokens; reasoning status 256 bytes | Low. |
| 4 | Any `error` forwarded; fatal only on 401/known codes | Any `error` ends the call | Low-Medium. Nexus is more tolerant. Keep, but log. |
| 5 | Voice instructions rewrite requests | Pass-through | See 7a. |
| 6 | Voice instructions avoid claiming completion and tell the model about approvals on screen | Fixed spoken sentence for approvals; nothing else | None; Nexus is richer. Keep. |
| 7 | No agent-side notice that input is speech | `realtime_start.md` / `realtime_end.md` developer fragments | Medium (7a.2). |
| 8 | No retire-on-safety-stop | `RealtimeHandoffAdmission` retires queued handoffs after a misalignment stop | Low for now. |
| 9 | `openai-session-id` fallback for call id | none | None; harmless to keep. |
| 10 | `originator: nexus` | `codex_cli_rs` (or the host app's name) | Unknown. If calls get rejected for identity, this is the first header to vary. Codex does not need attestation outside the desktop host. |
| 11 | Delegation parts joined by `""` or `"\n"` per PN decision | `""` | Nexus chose `"\n"`; if a long delegation arrives in several parts and reads oddly, switch to `""`. |
| 12 | No transcript flush at call end | Remaining untranscribed speech routed to the agent as `transcript_tail_flush` | Low-Medium: a final spoken sentence that never became a delegation is currently lost. |
| 13 | Delegation with empty content is rejected ("request was empty") | Falls back to the accumulated transcript | Medium. Match Codex: use the transcript when the content is empty. |
| 14 | Uses `input_transcript.added` as cumulative text (observed) | Appends it as a delta; `turn.done` replaces | Check on a raw log; one of the two is wrong for this endpoint. |
| 15 | One agent steer per delegation when busy | `StartOrSteer`: the same text steers a running turn | Matches Nexus's queue/steer; no change. |
| 16 | Voices: nine | 25 for v3 | Low; consider widening the allow-list after testing. |

---

## 8. Open questions this repo did not settle

- Whether the server's `delegation.created.item.content` is ever model-authored on
  `gpt-live-1-codex`. No fixture or prose says. Needs a raw-frame spike.
- The effect of `delegation.ack_filler`.
- Whether the sideband or the media leg is what closes at ~35 s (section 2.2).
- Whether the server requires attestation on some plans or regions (Codex only sends it from the
  desktop host).
- Why `voice-host/README.md` says voice is not enabled in the TUI while the TUI code drives it.
