# GPT Live realtime protocol notes

Status: research notes, nothing implemented. Written 2026-10-13 from two public
reference integrations, read at pinned commits. Purpose: let an engineer
re-implement the call protocol in Python from these notes, without copying code.
Everything here is observed behavior of undocumented endpoints. Treat it as
"what the references do", not as an API contract.

## 0. Sources and citation key

| Key | Repository | Commit |
| --- | --- | --- |
| **OC** | `malhashemi/opencode-gpt-live` (MIT) | `c23101cfdeb3feaf03f09aa678135ca01d72ed4e` |
| **OMP** | `can1357/oh-my-pi` (MIT) | `818dd42b73ab1caad17fa06d4edcc3a97086dc57` |

Citations are `KEY path:line` with line numbers at those commits. Example:
`OC src/server/live.ts:11` is the call URL constant in OpenCode's `live.ts`.

Read but not cited in detail: OC `docs/architecture.md`, `README.md`, `LICENSE`;
OMP `docs/rpc.md` (live section, around lines 1360-1402), `crates/pi-voice/src/live.rs`,
`packages/coding-agent/src/live/{transport,protocol,controller,attestation,voices}.ts`,
`packages/coding-agent/src/live/prompts/*.md`.

Not read, so anything depending on them is marked UNCONFIRMED:
- OC `src/shared/rpc.ts`, `src/server/prompts/*`, `src/server/context.ts`, and the
  native helper (`native/src/transport.rs`, `native/src/main.rs`). The helper
  is only described in `docs/architecture.md`.
- OMP `packages/pi-catalog/src/wire/codex.ts` (`CODEX_BASE_URL`, `OPENAI_HEADERS`,
  `CODEX_CLIENT_VERSION`), `pi-ai` (`withOAuthAccess`), `pi-utils` (`prompt.render`).

Summary of the answers that matter most:
- The call is **JSON** (`{"sdp", "session"}`), not multipart, in both references.
- The answer SDP is the **raw response body**. The call id is in the **`Location`** header.
- The sideband is `wss://api.openai.com/v1/live/<call id>` with the **same auth headers** as the POST.
- **Attestation is not required by either reference.** OC never sends it. OMP sends it only when
  it can generate it (macOS arm64 DeviceCheck), and omits the header otherwise. Whether the server
  rejects calls without it is UNCONFIRMED (neither reference was run).

---

## 1. Signaling: call creation

### 1.1 Request

- Method: `POST`.
- URL: `https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`
  (OC `src/server/live.ts:11`). OMP builds the same path from `CODEX_BASE_URL`
  (OMP `transport.ts:18`). The base value is UNCONFIRMED from the fetched files, but it is
  consistent with OC's literal URL.

Headers. Values are as sent by each reference.

| Header | OC value | OMP value | Refs |
| --- | --- | --- | --- |
| `Authorization` | `Bearer <ChatGPT OAuth access token>` | same form | OC `live.ts:54`, OMP `transport.ts:86` |
| `ChatGPT-Account-ID` | the ChatGPT account id | same, via `OPENAI_HEADERS.ACCOUNT_ID` (string UNCONFIRMED) | OC `live.ts:55`, OMP `transport.ts:95-96` |
| `OpenAI-Alpha` | `quicksilver=v2` | `quicksilver=v2` | OC `live.ts:56`, OMP `transport.ts:87` |
| `session-id` | random UUID per call (the "session" id) | the Nexus session id, passed as `sessionId` (header name UNCONFIRMED for OMP) | OC `live.ts:57`, OMP `transport.ts:92` |
| `thread-id` | a second random UUID | same value as `session-id` | OC `live.ts:58`, OMP `transport.ts:93` |
| `x-session-id` | random UUID per call (the "realtime session" id, one per call) | random UUID per transport | OC `live.ts:59`, OMP `transport.ts:89`, `transport.ts:117` |
| `originator` | `opencode` | `Codex Desktop` | OC `live.ts:60`, OMP `transport.ts:23`, `:90` |
| `User-Agent` | `opencode-gpt-live/<package version>` | `Codex Desktop/<CODEX_CLIENT_VERSION>` | OC `live.ts:61`, OMP `transport.ts:88` |
| `Content-Type` | `application/json` | `application/json` | OC `live.ts:98`, OMP `transport.ts:204` |
| `Accept` | not set | `*/*` | OMP `transport.ts:203` |
| version header | not set | `OPENAI_HEADERS.VERSION = CODEX_CLIENT_VERSION` (name UNCONFIRMED) | OMP `transport.ts:91` |
| `x-oai-attestation` | never sent | sent only if attestation was generated | OMP `transport.ts:97`, see §6 |

Notes:
- OC's `headers()` builds the auth set once. It is reused for the sideband (`live.ts:256`).
- The `originator` and `User-Agent` values identify the client to the server. Pick
  them deliberately and record the choice in `docs/decisions.md` when implementing. The
  references use two different identities (OC: its own name; OMP: the Codex desktop app).
  Neither is documented.
- Account id source. OC reads `metadata.accountID` from the OAuth credential first, then
  the JWT claim `https://api.openai.com/auth`.`chatgpt_account_id`, then a top-level
  `chatgpt_account_id` claim (`OC src/server/auth.ts:14-27`, `:55-59`). OMP uses
  `access.accountId ?? getCodexAccountId(token)` (`OMP transport.ts:95`). The second
  function is not in the fetched files (UNCONFIRMED), but it is presumably the same JWT claim.
- OC skips any credential that has no account id (`auth.ts:59`) and reports
  "sign in again" if none is usable (`auth.ts:62`).

### 1.2 Request body

JSON object with two keys (OC `live.ts:99`; OMP `transport.ts:210-213`):

```
{ "sdp": "<SDP offer text>", "session": <session object> }
```

- `sdp` is the full SDP offer as a string, not base64.
- The OMP `protocol.ts:165` comment says "multipart". The code (`transport.ts:201-215`) sends
  `application/json`. Trust the code.

### 1.3 Session object

Built in OC `index.ts:292-305` and OMP `protocol.ts:14-19`, `protocol.ts:166-173`:

| Field | Value | Refs |
| --- | --- | --- |
| `model` | `"gpt-live-1-codex"` | OC `live.ts:10`, OMP `protocol.ts:2` |
| `instructions` | the rendered live prompt (OC appends conversation history, see below) | OC `index.ts:294-302`, OMP `controller.ts:163` |
| `audio.output.voice` | voice name string (§7) | OC `index.ts:303`, OMP `protocol.ts:17` |
| `delegation.type` | `"client"` | OC `index.ts:304`, OMP `protocol.ts:18` |

What the references do NOT send:
- No input audio format, output audio format, sample rate, or codec. Audio format is
  negotiated by SDP (Opus, §5).
- No turn-detection (VAD) setting, no tools list, no temperature, no max tokens.
  Server defaults for these are UNCONFIRMED.
- No `session.update`. Neither reference ever sends one (§4.6).
- OC's `LiveSession` type declares `delegation.ack_filler?: boolean` (`live.ts:31`) but
  never sets it. Its effect is UNCONFIRMED.

OC appends conversation history to `instructions`: the previous calls' turns
(bounded to 4,000 characters, `index.ts:296-301`) and the current main-session
history (4,000 characters, `index.ts:302`). This is bounded background text that
the references present as quoted data.

### 1.4 Response

- Status: success is HTTP 2xx. Error statuses are handled in §8.
- Body: the **raw SDP answer as text**, not JSON (OC `live.ts:108`; OMP `transport.ts:216-221`).
  A blank body is an error (OC `live.ts:109`; OMP `transport.ts:222-223`).
- Call id: from the **`Location` response header**. Procedure:
  1. Take the Location value, resolve it against the call URL if relative, and drop
     the query string.
  2. Split the path on `/` and take the first segment that matches the call-id pattern.
  3. OMP's pattern is `^rtc_[\w-]+$` (OMP `transport.ts:24`, `:64-70`).
  4. OC accepts that pattern **or** a canonical UUID (OC `live.ts:14`, `:77-82`).
  5. OC only: if Location gives nothing, it falls back to the response header
     `openai-session-id` if it matches the same pattern (OC `live.ts:83-84`, `:107`).
     OMP has no fallback and raises "no valid call ID" (OMP `transport.ts:224-227`).
- The call id is used only to build the sideband URL (§2). OMP stores it on the
  transport and reuses it for every sideband attempt.

---

## 2. Sideband WebSocket

### 2.1 URL

`wss://api.openai.com/v1/live/<call id>`, with the call id percent-encoded
(OC `live.ts:12`, `:251`; OMP `transport.ts:73-77`, which builds an `https` URL with
`encodeURIComponent` and switches the scheme to `wss:`).

### 2.2 Headers and subprotocols

- The handshake carries the **same header set** as the POST, including
  `Authorization`, `ChatGPT-Account-ID`, `OpenAI-Alpha`, `session-id`, `thread-id`,
  `x-session-id`, `originator`, `User-Agent`, and `x-oai-attestation` if present.
  Sources: OC `live.ts:256` (`headers(...)`), OMP `transport.ts:249`.
- No `Content-Type` or `Accept`, because it is a WebSocket upgrade.
- No subprotocol is requested in either reference (OC `live.ts:271`, OMP `transport.ts:252`
  pass only the headers option). Whether the server needs one is UNCONFIRMED.
- Both references pass the headers at socket construction. A browser `WebSocket`
  cannot do that, so the Python client must use a library that accepts handshake
  headers. Parameter names differ between library versions. Check the version you
  pin. This is general library knowledge, not from the references.

### 2.3 Connection behavior

- Retry: up to 5 attempts. Sleep `200 * 2^n` ms between attempts (`n` = 0..3), i.e. 200,
  400, 800, 1600 ms (OC `live.ts:259-262`; OMP `transport.ts:231-243`). Each attempt
  has a 15-second timeout (OC `live.ts:275`; OMP `transport.ts:21`, `:318-321`).
- OC retries on any error, including a 401 handshake. It does not distinguish
  auth failures from transient ones (`live.ts:253-265`).
- Ordering differs:
  - OMP waits until the WebRTC data channel is open **before** opening the sideband
    (`transport.ts:175-177`).
  - OC returns the SDP answer to its client immediately and opens the sideband in the
    background, in parallel with ICE (OC `index.ts:337-339`, `:398-400`).
- Binary frames are unexpected. OMP reports them as a failure (`transport.ts:287-290`).
  All messages are JSON text (§3).
- Close without an error: when the socket closes while the call is live, OC reports
  "closed" (`live.ts:300-303`). OMP reports a failure if state is connecting or connected
  (`transport.ts:302-313`).

---

## 3. Inbound events (sideband and data channel)

Every message is a JSON object with a string `type`. Anything else is dropped
(OC `live.ts:136-143`; OMP `protocol.ts:141-163`). Unknown types are kept as "other"
and ignored. Neither reference treats an unknown type as an error.

Events are shown with the fields the references read. "OC" and "OMP" say what each
reference does with the event.

### 3.1 Session lifecycle

- **`session.started`**: fields `session.id` (string), optional `session.instructions`.
  - OC: `started` (`live.ts:146-147`). The bridge does not use it (`bridge.ts:150-173`).
  - OMP: moves the phase to `listening` (`controller.ts:279-281`).
- **`session.updated`**: same shape as `session.started`.
  - OC: falls through to "other", ignored (`live.ts:194-195`).
  - OMP: parsed (`protocol.ts:39`, `:147`) and ignored (`controller.ts:282-285`).
- **`session.created`**: referenced by neither. The Nexus plan lists it, so drop it unless
  a spike shows it on the wire. Not verified.
- **`session.closed`**: optional `reason` string. OC: `closed`, which ends the call
  (`live.ts:184-185`; `index.ts:383`). OMP: not in its parser, so it is `unknown`.

### 3.2 Transcripts

- **`input_transcript.added`** and **`output_transcript.added`**: `item.text` (string).
  - OC: `transcript` with role `user` for input and `assistant` for output, `final: false`
    (`live.ts:148-153`).
  - OMP: `#addTranscript` (`controller.ts:385-405`). The code handles three
    possibilities: the new text extends the previous text as a prefix, it ends with the
    previous text, or it is a new fragment to append. Whether these events are cumulative
    snapshots or deltas is UNCONFIRMED. Handle both.
- **`session.input_transcript.delta`** and **`session.output_transcript.delta`**: field
  `delta` (string). Partial text.
  - OC: parsed as non-final transcript text (`live.ts:154-163`).
  - OMP: not handled. It parses as `unknown` (`protocol.ts:160-161`). **Discrepancy.**
    A re-implementation should decide whether to use these. OC is the only reference that does.
- **`turn.done`**: field `turn` with `role` (`"user"` or `"assistant"`) and `transcript`
  (string). This is the final text for that turn.
  - OC: `transcript` with `final: true` (`live.ts:164-169`). The bridge then keeps the
    last 40 turns and persists them (`bridge.ts:153-159`).
  - OMP: `#finishTranscript` (`controller.ts:407-419`).

### 3.3 Delegation

- **`delegation.created`**: field `item`, which is
  `{type: "delegation", target: "client", id: <string>, content: [{type: "input_text", text: <string>}, ...]}`.
  - Validation: OC requires `item.type == "delegation"`, `item.target == "client"`, string
    `item.id`, and reads `content` as a list (`live.ts:170-181`). OMP requires the same
    fields and content must be a list (`protocol.ts:103-118`).
  - Request text: concatenate the `input_text` parts. **OC joins with an empty string**
    (`live.ts:175-179`). **OMP joins with newlines** (`controller.ts:305-310`).
    This is a discrepancy. Pick one. The difference only matters when the model
    sends several parts.
  - The `item.id` is the delegation id. Every reply to this delegation carries it as
    `delegation_item_id` (§4.2).

### 3.4 Errors

- **`error`**: the message can be in several places. OMP's parser tries, in order:
  top-level `message` string; `error.message` string; `error` as a string; `error` as any
  other JSON value, stringified (`protocol.ts:130-138`). OC tries `error.message`, then
  `message`, then `error` as a string, then the fallback "GPT-Live error"
  (`live.ts:128-134`).
  - OC also derives a `fatal` flag: true when the status is 401, or when the code (top level or
    under `error`, lowercased) is one of `authentication_error`, `invalid_token`,
    `token_expired`, `invalid_api_key` (`live.ts:186-193`). The bridge only forwards the
    message and ignores the flag (`bridge.ts:169-171`).
  - OMP treats **every** `error` event as terminal. It reports a failure, which ends the
    call (`controller.ts:298-299`, `:515-521`).

### 3.5 Ignored or unused by both

- **`output_audio.delta`** (field `audio`, string): OMP parses it, then ignores it
  (`protocol.ts:42`, `:149-150`; `controller.ts:283`). Audio arrives on the media track, so
  the sideband copy is not needed. OC: unknown, ignored.
- **`output_audio_buffer.cleared`**: OC maps it to `speech-cleared` (`live.ts:182-183`), and the bridge
  ignores it (`bridge.ts:150-173`). OMP: unknown.
- Anything else: unknown or "other", ignored.

---

## 4. Outbound messages (sideband)

All client messages are JSON text frames, sent in order. OMP serialises sends on a
promise chain (`transport.ts:363-374`; `controller.ts:451-459`). OC calls `socket.send`
directly (`live.ts:308-310`) and drops the message if the socket is not OPEN.

### 4.1 Session context append (no delegation)

```
{ "type": "session.context.append", "channel": "speakable" | "commentary" (optional),
  "content": [ { "type": "input_text", "text": "<chunk>" } ] }
```
Source: OC `live.ts:226-227` (the non-delegation branch of `contextAppend`); OMP
`protocol.ts:30-33`, `:190-196`. OMP omits `channel` when it is undefined (`protocol.ts:192`).

Used by OC for announcements (task outcomes, permission requests, lost event stream) and
for "The user typed: <text>" (`bridge.ts:470-473`, `:492-494`).

### 4.2 Delegation context append (reply to one delegation)

```
{ "type": "delegation.context.append", "delegation_item_id": "<item.id>",
  "channel": "speakable" | "commentary" (optional),
  "content": [ { "type": "input_text", "text": "<chunk>" } ] }
```
Source: OC `live.ts:225-226`; OMP `protocol.ts:24-27`, `:176-187`.

Used by OC for the voice agent's reply to a hand-off (`bridge.ts:371`). Used by OMP for progress
and the final answer (`controller.ts:342`, `:356`).

### 4.3 Channel values

- `"speakable"`: the model may say it aloud. OC uses it for announcements and for the
  voice reply to a user message (`bridge.ts:369`, `:471`). OMP has the type
  (`protocol.ts:8`) but uses it only implicitly (see below).
- `"commentary"`: progress context that the model should not recite. OC uses it for voice
  replies after a delegation (`bridge.ts:369`). OMP uses it for each intermediate tool-step
  message (`controller.ts:341-343`). The OMP prompt says "silent progress; never recite"
  (`prompts/live-instructions.md` line 17).
- **No channel**: OMP sends the final answer without a `channel` (`controller.ts:356`).
  Its default on the server is UNCONFIRMED. The OMP prompt expects the model to present
  the final answer naturally, so it is probably treated as speakable.

### 4.4 Chunking rule: 500 UTF-8 bytes per append

- Each append carries at most **500 UTF-8 bytes** of text, counted per code point
  (OC `live.ts:13`, `:199-218`; OMP `protocol.ts:5`, `:203-233`).
- Rule, same in both: walk the string by code point. Add each code point's UTF-8 length
  (1 to 4 bytes) to the current chunk. If the next code point would make the chunk exceed
  500 bytes, close the chunk and start a new one. Never split a code point.
- Text that is already ≤ 500 bytes is one chunk. Empty input gives one empty chunk
  (OMP `protocol.ts:212`). Callers in both references skip empty text before chunking.
- Each chunk becomes its own append message, in order (OC `live.ts:222-228`;
  OMP `controller.ts:341-343`, `:355-357`).
- Chunks are split on code points, not grapheme clusters. A combining sequence can be
  split across two appends. Both references accept that.

### 4.5 "Agent Final Message" prefix (OMP only)

When a delegated turn ends, OMP renders this template, then chunks the result (§4.4):

```
"Agent Final Message":

<final assistant text>
```
Source: `prompts/agent-final-message.md` lines 1-3 and `controller.ts:354-357`. The
final text is the last assistant message in `agent_end` that has non-empty visible text
(`controller.ts:346-353`). The rendered text is chunked, so the prefix sits in the first chunk.

Unknowns about the template engine: `prompt.render` is from `pi-utils`, not fetched.
If it is Handlebars, `{{message}}` would be HTML-escaped by default (UNCONFIRMED). A Python
re-implementation should insert the text raw, without escaping.

Intermediate progress is sent when an assistant message ends with `stopReason == "toolUse"`
(`controller.ts:328-330`, `:336-344`). Only its visible text is sent, on the `commentary`
channel.

OC has no label and no template. It sends plain sentences, such as
`Finished the task "<label>". Outcome: <result>` or
`The task "<label>" failed: <detail>` (`bridge.ts:48-60`, `:470-473`). The task label is clipped to 120
characters and error detail to 300 (`bridge.ts:47-58`). The voice agent's reply is capped at
1,200 characters (`bridge.ts:363`). The main session's result is passed through `speakable()`
with no length cap (`bridge.ts:454`).

### 4.6 Session close, hang-up, mute, session.update

- **Session close**: `{"type": "session.close"}` (OC `live.ts:319`; OMP `protocol.ts:34`,
  `:199-201`). The references send it **before** closing the socket:
  - OC: sends it, then closes the socket with code **1000** and reason `"session closed"`
    after 250 ms (`live.ts:316-321`).
  - OMP: the controller sends it (`controller.ts:252-254`), then the transport closes the
    socket with code 1000 and reason `"done"` (`transport.ts:397-405`).
- **Hang-up by voice** (OC only): the voice agent calls an `end_call` tool. The call ends
  4.5 s later, which gives the model time to say goodbye (`bridge.ts:483-489`). Not a
  sideband message.
- **Mute**: **no sideband message in either reference.** Mute is local:
  - OMP disables the native media sender (`transport.ts:383-386` → `live.rs:308`) and
    stops pushing microphone samples (`controller.ts:370-372`).
  - OC mute lives in its native helper. The helper is not fetched, so this is UNCONFIRMED.
- **`session.update`**: neither reference sends one. Whether it exists or is accepted is
  UNCONFIRMED.

---

## 5. WebRTC specifics

### 5.1 Peer and offer (OMP, the only reference with the source in hand)

Source: `crates/pi-voice/src/live.rs`.

- Media engine: one Opus codec registered with payload type **111** (`live.rs:158`).
  Default interceptors are registered (`live.rs:150-170`).
- Peer configuration: default. No ICE servers are configured (`live.rs:171`,
  `RTCConfiguration::default()`).
- One **local audio track**: id `"audio"`, stream id `"omp-live"` (`live.rs:176-180`).
- One **data channel** labelled **`oai-events`**, created with default options
  (`live.rs:49`, `:193`). This is where the server sends the same JSON events as the
  sideband (`live.rs:91`).
- Offer: `create_offer` then `set_local_description`. The resulting SDP text is returned
  as is (`live.rs:202-226`). **No SDP munging.** The answer is applied as received
  (`live.rs:229-243`).
- Whether the offer includes ICE candidates depends on whether the implementation waits
  for gathering to finish. The reference does not wait explicitly (`live.rs:202-226`).
  Whether the server needs trickle or a complete offer is UNCONFIRMED. Test this in a spike.
- The data channel must open within **20 s** (`live.rs:61`, `:247`). OMP then waits for
  it (`transport.ts:175`).

### 5.2 Audio codec and rates

| Direction | Value | Refs |
| --- | --- | --- |
| Codec | Opus, mime `audio/opus` | `live.rs:65-70`, `:382-387` |
| RTP clock rate | **48000** | `live.rs:55`, `:67` (`clock_rate`) |
| Channels declared | **2** in the codec capability (SDP `opus/48000/2`) | `live.rs:68`, `:386` |
| fmtp | `minptime=10;useinbandfec=1` | `live.rs:387` |
| Encoder input | **mono, 16 kHz**, `Application::Voip`, in-band FEC on | `live.rs:498`, `:507`; input rate `live.rs:50` |
| Frame | **20 ms = 320 samples** at 16 kHz | `live.rs:51-52`, `:557` |
| Decoder output | 48 kHz, mono | `live.rs:55`, `:614` |
| Controller mic capture | 16 kHz mono Float32 | OMP `controller.ts:186`; `transport.ts:376` |

Notes:
- OMP encodes 16 kHz mono PCM as Opus while the RTP clock is 48 kHz. The packets are
  valid Opus, and the reference works with them.
- The remote track must negotiate `audio/opus` (`live.rs:596-609`). Otherwise the
  reference fails with "unsupported audio codec".
- OC: capture at 48 kHz, echo cancellation and noise suppression, Opus with 20 ms frames,
  and 48 kHz playback (`docs/architecture.md:115-116`). The channel count and the
  data-channel label in OC are UNCONFIRMED (native helper not fetched).

### 5.3 Data channel messages

- Text payloads only. OMP handles them as `oai-events` text (`live.rs:91`).
- Same JSON as the sideband. OMP forwards data-channel events to the controller
  **only until the sideband is open**, except `error` events (`transport.ts:336-343`).
  After that, the sideband is the single source. A Python client can take the same
  approach, or use the data channel as the only source. The references use the sideband.

### 5.4 Echo handling (client-side, not protocol)

- OMP's controller drops microphone audio while output is active, unless the mic level
  clears a threshold (`controller.ts:22-24`, `:370-383`):
  `OUTPUT_ACTIVE_LEVEL = 0.015`, `MIN_BARGE_IN_LEVEL = 0.04`, `OUTPUT_ECHO_RATIO = 0.65`.
- OC uses real echo cancellation in the native helper (`docs/architecture.md:115`).

---

## 6. Attestation

What it is:
- An optional request header, `x-oai-attestation`, sent on the POST (§1.1) and on the
  sideband handshake (OMP `transport.ts:97`, `:249`).
- Its value is a JSON envelope `{"v": 1, "s": 0, "t": "v1.<base64url token>"}`
  (OMP `attestation.ts:90`).
- The token is an Apple DeviceCheck token from the device, plus a small set of
  device and locale signals. It names the ChatGPT desktop app's bundle identifier
  (OMP `attestation.ts:3`, `:62-79`). The encoding details are not needed to
  re-implement the transport; they are in the reference and should not be copied.

How it is generated:
- Only on **macOS on arm64** (OMP `attestation.ts:83`). Otherwise it returns `undefined`.
- If DeviceCheck throws or is unsupported, it also returns `undefined` (`attestation.ts:84-89`).
- When `undefined`, the header is simply not sent (`transport.ts:97`).

Is it mandatory? From the references:
- **OC never sends it** (`live.ts:52-63` has no such header). Its README says the voice call
  works with a ChatGPT subscription (`README.md:55-57`). Not verified by running it.
- **OMP treats it as optional**, and sends the call without it when generation fails
  (`transport.ts:97`, `attestation.ts:83-89`).
- Conclusion: neither reference requires attestation in its code. Whether the server
  rejects calls without it in some cases (for example, a plan or region check) is
  UNCONFIRMED. Verify in a spike, and treat any 403 as possibly related until it is ruled out.
- On Linux and Windows the reference cannot produce the token at all.
- This matches the Nexus plan's default: omit it unless the spike shows it is required.

---

## 7. Voices

Accepted voice names (nine), the same in both references:
`arbor`, `breeze`, `cove`, `ember`, `juniper`, `maple`, `sol`, `spruce`, `vale`
(OMP `voices.ts:2-12`; OC `README.md:140`).

Defaults:
- **OC**: `cove` (`index.ts:75`; `README.md:140`). The default is applied only when the
  configured voice is one of the nine (`index.ts:75`). A per-call `input.voice` is used
  without validation (`index.ts:234`).
- **OMP**: `sol` (`voices.ts:18`; `controller.ts:55`, `:132`).

Server-side behavior for an unknown voice name is UNCONFIRMED. Validate on the client.

---

## 8. Error handling

### 8.1 HTTP status from call creation

| Status | Meaning, per the references | Refs |
| --- | --- | --- |
| **401** | ChatGPT rejected the sign-in. OC message: "Sign in again" with its own connect command. | OC `live.ts:70` |
| **403** | The ChatGPT plan may not include voice, or the voice allowance is used up. | OC `live.ts:71-72`, README `:214` |
| **429** | Voice usage limit reached. Try later. | OC `live.ts:73` |
| other | Generic: "call creation failed (status): detail". | OC `live.ts:74` |

- OMP has no status-specific messages. It throws
  `Codex live signaling failed (<status>): <detail>` (`transport.ts:217-219`).
- OMP fetches the OAuth token through `withOAuthAccess` (`transport.ts:184-193`), which is
  in `pi-ai` and not fetched. Whether it refreshes on 401 is UNCONFIRMED.
- OC refreshes the token through OpenCode's host before each call (`auth.ts:31-34`, `:35-37`).

For Nexus, the plan maps 401 to "reconnect ChatGPT" and 403 to "plan or allowance".
The references also support mapping 429 to "try again later".

### 8.2 Bounding and redaction

- OC: the error body is redacted (the bearer token and account id are replaced with
  `[redacted]`), then cut to 300 characters (`live.ts:65-67`, `:103-105`).
- OMP: the error body is whitespace-collapsed and cut to 2,048 characters
  (`transport.ts:19`, `:101-106`). The transport does not redact secrets.
- Nexus policy says all errors go through `redact_secrets` before crossing the host
  boundary (AGENTS.md / the plan). Apply that to both.

### 8.3 Other failures

- Missing call id, or empty SDP answer: error in both (OC `live.ts:85`, `:109`;
  OMP `transport.ts:222-227`).
- Sideband failed after 5 attempts: error (OC `live.ts:259-260`; OMP `transport.ts:243`).
- Sideband `error` event during a call: OMP ends the call (`controller.ts:298-299`).
  OC forwards the message to the UI and keeps the call alive (`bridge.ts:169-171`).
- Transport error during the call: OMP reports it once and then stops (`transport.ts:352-360`).

---

## 9. Bundled live instructions (OMP prompt)

OC's `src/server/prompts/` was not fetched, so its prompt is UNCONFIRMED here. The
summary below is of OMP's `prompts/live-instructions.md` (lines 1-23) and
`prompts/agent-final-message.md`. Rules, paraphrased, not quoted:

- Persona: the voice front of one coding assistant. It is one assistant with two
  surfaces, not a separate agent.
- Speak briefly and conversationally. Use speech-friendly phrasing. No markdown, no
  code blocks, no long lists, and no implementation detail unless asked.
- Coding, investigation, repo changes, commands, or verification: **always delegate**,
  with a complete plain-language request that includes the relevant conversation.
  Never attempt the tool work directly.
- A new request while work is running creates a **new delegation**, which steers the same backend session.
- Treat delegation context as internal progress. The assistant may briefly acknowledge
  active work. It must **not claim changes, findings, or verification** before the
  backend reports.
- Commentary context is silent progress. Never recite it.
- Context that begins with the `"Agent Final Message":` label is the backend's final
  answer. Present it naturally as its own. Never mention the label, the protocol, the
  delegation, or the backend.
- Greetings, clarification, and small talk that need no tools: answer directly. Ask a
  short clarifying question only when the execution request is genuinely underspecified.
- Keep one assistant identity: converse here, delegate the work, report the result.

Two things the reference does that the Nexus plan says not to do:
- It renders the prompt with the **OS account name and first name**
  (`controller.ts:83-93`, `:163`). The Nexus plan renders with the workspace and agent
  names, not the OS user. Keep that.
- It lets an operator replace the whole prompt with a template (`controller.ts:61`, `:133`).
  The Nexus plan allows an override at `<workspace>/.agents/live/instructions.md`.

OC prompts (`gpt-live` and `voice-agent` sections) are UNCONFIRMED here. Its README describes
them as Markdown sections with `{{project}}` and `{{directory}}` placeholders (`README.md:171`).

---

## 10. Licenses

- **OC** `malhashemi/opencode-gpt-live`: MIT. Copyright (c) 2026 M. Adel Alhashemi
  (`LICENSE:1-3`; `README.md:249-251`). Its README also says the project is not affiliated
  with OpenAI and that ChatGPT and GPT-Live are OpenAI trademarks (`README.md:244-247`).
- **OMP** `can1357/oh-my-pi`: MIT. Copyright lines (`LICENSE:1-5`):
  - (c) 2025 Mario Zechner
  - (c) 2025-2026 Can Bölük
  - (c) 2026 Stencil Labs, Inc.

Both are permissive. A re-implementation written from these notes, without copying
code, does not carry their license obligations. If any code or prompt text is copied,
keep the MIT notice in `live/NOTICE`, as the Nexus plan already requires.

---

## Discrepancies to resolve in the implementation

| Topic | OC | OMP | Suggested choice |
| --- | --- | --- | --- |
| Delegation text join | `""` (`live.ts:175-179`) | `"\n"` (`controller.ts:305-310`) | `"\n"`, so separate parts stay readable |
| Transcript deltas | parsed (`live.ts:154-163`) | not parsed (`protocol.ts:160`) | parse them, but treat `turn.done` as the source of truth |
| Call id fallback | `openai-session-id` header (`live.ts:83-84`) | none (`transport.ts:224-227`) | fall back, but log which source was used |
| Attestation | never sent | sent when available (`transport.ts:97`) | omit (matches the Nexus plan), verify in a spike |
| Client identity | `originator: opencode` | `originator: Codex Desktop` | decide deliberately and record in `docs/decisions.md` |
| Auth errors on sideband | retried 5 times (`live.ts:253-265`) | retried 5 times (`transport.ts:231-243`) | do not retry 401; fail closed |
| Any `error` event | forwarded, call continues (`bridge.ts:169-171`) | ends the call (`controller.ts:298-299`) | forward and end the call only for `fatal` or 401/403 |
| Sideband vs data channel | sideband only | sideband, data channel until the sideband is open | sideband only, which avoids duplicate handling |
| Mic rate | 48 kHz (`architecture.md:115`) | 16 kHz Opus input (`live.rs:498`) | pick 48 kHz for simplicity; test both if audio quality is an issue |

---

## Open questions (UNCONFIRMED)

1. Is `x-oai-attestation` ever required by the server? Test it in the spike.
2. What is the default `channel` for an append that omits it?
3. Does `session.update` exist, and what does it change?
4. Are the transcript events cumulative or delta-based? Are the `*.delta` events emitted
   alongside the `*.added` events, or instead of them?
5. Does the server require ICE candidates in the offer, or does trickle work?
6. OC's native helper: data-channel label and channel count, and whether it munges SDP.
7. Server defaults for turn detection, input audio format, and tools when they are not set.
8. Does `withOAuthAccess` refresh on 401? Does OC's host refresh on each call (documented in its code comment only)?
9. Exact values of OMP's `OPENAI_HEADERS` names and `CODEX_BASE_URL`.
10. Whether `session.created` ever appears on the wire.
11. Whether the server validates voice names, and how it reports a bad one.
12. Whether OC's prompts or its `mute` implementation contain rules the Nexus design should copy in spirit (for example, stop and approval handling). Read `src/server/prompts/` and `native/` before deciding.


---

## Authorship of delegation text (researched later; why requests arrive verbatim)

Question: can the voice model send the agent its own wording?

Evidence:
- Nexus data: session `session-1ce5e575` (`~/.nexus/nexus.db`, table `records`): the
  `live.delegated` text at seq 30 ("Okay, how do we think about Uh GPT Live, uh taking the
  whole session context, how does that work here") equals the last cumulative
  `live.transcript.delta` for role user, speech-recognition noise ("Uh", "uh") included,
  although `live.started` (seq 7) recorded the bundled instructions with the rewrite rule.
  No `log_raw` frames exist on this machine (`~/.nexus/logs/live/` absent; `[live].log_raw`
  defaults to false, `nexus/config/schema.py:914`), so the raw frame was not inspected.
- OC `src/server/live.ts` (c23101c): `LiveSession` is `{model, instructions, audio, delegation:
  {type: "client"; ack_filler?: boolean}}`; no tools array or function-call event; `delegation.created`
  parsed into `{kind: "delegation", id, text}`. OC `bridge.ts`: `toVoice` wraps the text as
  `<request>...</request>`; `gptlive_end_call` appears only in `TOOL_LABELS` and as `endCall()`
  returning a string: it is a tool exposed to OC's own coding agent (the opencode voice session),
  not a tool declared to the voice model. The earlier note in section 4.6 ("the voice agent calls an
  `end_call` tool") should be read that way: it is not evidence of client function tools.
  `ack_filler` is declared but never set; effect unknown.
- OpenAI docs "Delegation and tools in GPT-Live" (developers.openai.com/api/docs/guides/live-delegation):
  client delegation is `{"model": "gpt-live-1", "delegation": {"type": "client"}}`;
  `session.delegation.created` "does not contain the user's utterance or task text"; the app builds
  the request from `session.input_transcript.delta` / `output_transcript.delta`; results go back with
  `session.commentary.append` / `session.thinking.append` / `session.instructions.append` carrying
  `delegation_id`. Function tools are documented only under `delegation.responses.tools` (Responses
  mode: OpenAI's backend runs the model, calls come back as `response.output_item.done`, answered with
  `response.item.create` `function_call_output` + `response.create`). The primary-websocket reference
  (first 100k chars read) lists session fields `model instructions audio delegation client input store
  triggers`; no top-level `tools`. That API (`gpt-live-1`, websocket, `session.start`) differs from the
  undocumented codex WebRTC endpoint Nexus uses, so it is only suggestive here.
- oh-my-pi @ 818dd42: the guessed protocol.ts URL returned 404; not re-checked.

Conclusion: there is no evidence that client function tools exist on this endpoint, and the one
observed delegation was the heard utterance. Nothing was implemented that assumes tools. Not verified:
whether the codex endpoint accepts a `tools` array or `delegation.responses` (a spike would need a
live call with `log_raw`), whether `ack_filler` changes anything, and whether the rewrite rule ever
changes the delegation text. Nexus now records `source`, `heard` and `verbatim` on `live.delegated`.
