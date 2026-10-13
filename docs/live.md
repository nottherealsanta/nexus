# GPT Live voice calls (`nexus/live/`)

Status: **M1 (daemon core), M2 (delegation bridge), M3 (the `nexus-live` helper), M4
(the native Ratatui client), and M5 (GPUI desktop and headless `nexus live`) are
implemented and tested offline. Nothing has been exercised against the real service,
a real microphone or a real call: not verified.** The desktop client draws the call
bar and card and routes `Ctrl+X D` (see [desktop.md](desktop.md#gpt-live-voice-calls));
a native-harness render check passed, but screenshot review has not run. Plan:
`plans/in-progress/GPT_LIVE_PLAN.md`; protocol observations and citations:
`plans/in-progress/GPT_LIVE_PROTOCOL_NOTES.md`.

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
| `live/protocol.py` | frozen shapes: call request, inbound events (`parse_event` is total, unrecognised frames become `Unknown`), outbound appends, `chunk_utf8` (<= 500 UTF-8 bytes, never inside a code point), the `"Agent Final Message":` prefix |
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
   the voice model is recorded as `live.appended`, text exactly as sent.
6. The call ends by `LiveStop`, helper exit, sideband loss, the voice service
   closing it, the deadline, no attached view for 30 s, the session being deleted,
   or daemon shutdown. Every path converges on one guarded `_finish` that sends
   `session.close` (<= 1 s), closes the socket and records **exactly one
   `live.ended`** per `live.started`. Ending a call never cancels the agent's
   running turn. When the session itself was deleted there is nothing left to
record into, so that one `live.ended` is logged instead of persisted. The facade
ends the call before it closes sessions on shutdown, so the event is durable.

States: `idle -> connecting -> active <-> muted -> closing -> idle`. A second
`LiveStart` fails with `live_busy` naming the owning session. Mute is local to the
client's helper; the daemon records it so every view agrees (no sideband message
exists for it).

## Client flow (Ratatui, M4)

`/live` (or `Ctrl+X D`; `Ctrl+X L` was already the details sidebar) toggles a call on
the session on screen. Code: `ui/ratatui/live.py` (bridge state) over
`ui_support/live_helper.py` (stdlib only; the desktop client and a CLI reuse it).

1. `LiveStatus`. Not available (`disabled`, `unsupported`, `no_credential`, `busy`)
   shows a panel with the host's message and the fix (`nexus auth codex login`,
   `pip install 'nexus-harness[live]'`, the session that owns a busy call).
2. The `nexus-live` binary must exist (`NEXUS_LIVE_BINARY`, the venv's bin, `rust/live/target`,
   `PATH`); otherwise an `unsupported` panel says to build or install it. Asked before consent.
3. First use only: a consent dialog says that microphone audio and summaries of the
   agent's work go to OpenAI through the user's ChatGPT account. Agreeing calls
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
| `/live mute` | toggle the microphone (`LiveMute`; the helper mutes, the daemon records it) |
| `/live new` | start a call without the carried-over turns (`new_conversation`) |
| `/live status` | availability, state, consent, helper installed, last error |
| `/live revoke` | withdraw the data-sharing consent |

**Call bar.** While a call is open the blank row above the composer controls reads
`● Live · <model> · <phase> · mm:ss   mic ▰▰▱▱▱▱ spk ▱▱▱▱▱▱   /live stop`, plus a
`MUTED` badge, and `· in <session>` when another session is on screen (the microphone stays
visible everywhere). The editor is untouched: typing during a call is ordinary steering.

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
reconnect or replay shows the same thing (`tests/test_ratatui_live.py`).

## Delegation bridge (M2)

A delegation is **untrusted input**, exactly like a typed prompt.

| Situation | Behaviour |
| --- | --- |
| idle session | `HostFacade.enqueue(mode="queue")`: a new turn |
| a turn is running | `enqueue(mode="steer")`: consumed at the next model step |
| the user message | its `input.queued` event carries `origin = {"kind": "live", "delegation_id"}`; the text and the turn are identical to a typed prompt, clients label it "via voice" |
| progress | `tool.requested` bursts become one `commentary` append at most every 5 s (`[live].progress`) |
| `turn.completed` | last visible assistant text, prefixed `"Agent Final Message":`, appended on the delegation (<= 12,000 characters, the omission is announced) |
| `turn.failed` / `turn.cancelled` | a short notice append |
| several delegations in one turn | the last gets the final answer; earlier ones get a "merged into a later request" notice |
| `permission.requested` / `question.requested` | a notice that the user must answer **on screen**. The bridge never resolves, grants or auto-approves anything; a heard "yes" is never a permission |

The bridge watches the session through `Session.observe()`, a tap that is **not**
a viewer, so it can neither make an unattended session look attended nor change
the unattended approval policy. Sub-agent events are ignored.

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

`LiveStatus`, `LiveStart`, `LiveMute`, `LiveStop`, `LiveHelperEvent` (see
[host.md](host.md)); results `LiveStatusResult` (also returned by mute and stop),
`LiveStartResult`, `LiveHelperEventResult`. Errors are `ErrorResult(kind, message)`
with a stable `kind`: `live_disabled`, `live_consent_required`, `live_busy`, `live_no_session`,
`live_bad_offer`, `live_bad_voice`, `live_instructions`, `live_auth` (401),
`live_forbidden` (403), `live_rate_limited` (429), `live_signaling`,
`live_no_call_id`, `live_timeout`, `live_not_active`, `live_unavailable`.
`LiveHelperEvent.kind` is one of `ready levels phase dc_error dropped closed`;
`detail` is bounded to 2 KiB. Client methods: `live_status`, `live_start`,
`live_mute`, `live_stop`, `live_helper_event` in `client/protocol.py`.

`LiveStatusResult.reason` is `disabled`, `unsupported` (the optional `websockets`
client is missing), `no_credential` (no ChatGPT sign-in; `nexus auth codex login`)
or `busy`.

## Config

See [config.md](config.md): `[live]` `enabled voice max_minutes carry_turns progress
instructions log_raw`, and `NEXUS_LIVE=off`. Install the WebSocket client with the
`live` extra (`pip install 'nexus-harness[live]'`); it is imported lazily and is not
a base dependency. The model id is a pinned constant, not config.

## Instructions and continuity

The bundled prompt is written for Nexus (not copied from the references) and is
rendered with the **workspace folder name and the agent name, never the OS user**.
Override with `[live].instructions` or `<workspace>/.agents/live/instructions.md`
(<= 32 KiB, `{{agent}}` and `{{workspace}}` placeholders). The rendered text is the
`instructions` field of `live.started`: what the voice model is told is shown.

Continuity: one voice conversation per Nexus session. The last `carry_turns` (12)
final transcript turns of earlier calls, trimmed to 8 KiB, are appended to the
instructions as quoted background and counted in `live.started.carried_turns`.
`new_conversation` starts empty, and later calls then carry only turns after it.

## Events and view

Durable, turn-less, one `call_id` per call (see [events-and-view.md](events-and-view.md)):

| Event | Data |
| --- | --- |
| `live.started` | `model voice instructions_source instructions carried_turns new_conversation` |
| `live.transcript` | final only: `role text turn truncated` |
| `live.transcript.delta` | the current partial caption `role text`; coalesced to <= 4 Hz |
| `live.delegated` | `delegation_id text mode queued_id` |
| `live.appended` | `delegation_id channel(commentary/final/notice) text chunks`, text exactly as sent |
| `live.phase` | `connecting listening speaking working muted`; <= 4 Hz |
| `live.error` | a redacted non-fatal error, <= 1 per second |
| `live.levels` | reserved, never persisted (see decisions) |
| `live.ended` | `reason duration_s error` (redacted), exactly one per `live.started` |

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
