# GPT Live voice calls (`/live`)

Status: **plan, nothing implemented.** Written 2026-10-13 from two reference
integrations: the OpenCode plugin `malhashemi/opencode-gpt-live` (commit
`c23101cf`) and oh-my-pi's Live (`can1357/oh-my-pi`, commit `818dd42b`). Neither
was run; their behavior is taken from their source and docs.

## 1. Goal

A real-time spoken conversation with OpenAI's `gpt-live-1-codex` model in any
Nexus terminal or desktop client. The voice model talks; **the Nexus session the
user already has open does the work** with its own model, tools and permissions.
The voice model never runs a Nexus tool directly.

```
 mic/speaker ⇄ nexus-live helper ⇄ (WebRTC, Opus) ⇄ OpenAI realtime
                    ▲ stdio JSONL                       ▲ sideband WebSocket
               client (TUI/desktop)                     │ (control, transcripts,
                    ▲ host contract                     │  delegations, appends)
                 daemon: LiveManager ──────────────────┘
                    │ enqueue delegation / read results
                 Session (normal turn: model, tools, approvals)
```

Non-goals for v1: the web client (being deprecated), a second "voice agent"
session with its own tools (OpenCode's design; see §3), voice-driven
approvals, Windows.

## 2. Hard constraints from Nexus

| Constraint | Consequence for Live |
| --- | --- |
| Credentials never leave the daemon (`docs/security.md`) | Signaling (OAuth bearer, attestation) and the sideband WebSocket live in the daemon. The audio helper only ever sees SDP. |
| All UI work through the host contract (AGENTS rule 4) | New `Live*` commands in `host/protocol.py`; clients never talk to OpenAI. |
| Durable log first (rule 5) | Final transcripts, delegations and call lifecycle are session events; replay rebuilds the call transcript. |
| One-way layering (rule 1) | `nexus/live/` is a manager (beside `voice/`); `core/loop.py` learns nothing about it. |
| UI encapsulation (rule 2) | Clients spawn the helper with stdlib `subprocess`; no audio/WebRTC library is imported under `nexus/ui/**`. |
| Show everything, never hide what the agent sees | Every delegation, every append sent back to the voice model, and the voice instructions are visible rows, labelled. |
| Bounded everything | Chunk sizes, queue depths, call length, transcript sizes, reconnect attempts all capped. |
| Undocumented protocol | Same caveat as `auth/codex.py`: pin to a cited commit, put it in `auth/NOTICE` / `live/NOTICE`, mark "experimental", fail closed. |

Existing pieces to reuse:

- `nexus/auth/codex.py` `CodexOAuthManager.headers()`: the ChatGPT OAuth bearer
  + account routing headers. Live uses the `openai-codex` / `chatgpt_oauth`
  credential profile; no new sign-in flow.
- `HostFacade.enqueue(session, content, mode=…)`: delegation becomes an
  ordinary queued/steered user input.
- `ui_support/voice_capture.py` cue playback, `sounddevice` already a base dep
  (cues only; the call audio goes through the helper).
- setuptools-rust `[[tool.setuptools-rust.bins]]` already ships `nexus-ratatui`;
  `nexus-live` ships the same way.
- Key/command slots: `/voice` and `Ctrl+X V` are dictation. Live takes
  **`/live`** and **`Ctrl+X L`** (free today; verify against `ui_support/shortcuts.py`).

## 3. Design decision: delegation model

Two shapes exist in the references:

- **OpenCode:** voice model → a hidden *voice session* (an LLM, `voiceModel`,
  default `openai/gpt-6.1-sol`) with only `gptlive_*` control tools → main
  session. Powerful (status, steer, stop, approvals by voice) but adds a second
  model, a second session, and a reasoning hop on every request.
- **oh-my-pi:** the realtime service emits `delegation.created`; the harness
  submits its text to the existing session and appends progress/final text back
  to that delegation.

**Recommendation: oh-my-pi's shape for v1.** It maps directly onto Nexus
primitives (enqueue + events), costs no extra model, and keeps everything the
voice model "did" visible as a plain user message in the transcript. Status,
steer and stop are covered by Nexus mechanics instead of tools:

| Spoken intent | v1 mechanism |
| --- | --- |
| "Do X" while idle | delegation → `enqueue(mode="queue")` → new turn |
| "Also / instead do Y" while a turn runs | delegation → `enqueue(mode="steer")` (consumed at the next model step) |
| "Stop" | the voice instructions tell the model to delegate the literal text `stop`; LiveManager recognises a delegation whose normalised text is exactly a stop phrase **and** confirms it in the UI, then `SessionCancel`. Anything fuzzier is just steering. (Decide in M3 whether this is worth it; the user can always press Esc.) |
| "How is it going?" | LiveManager already appends progress commentary; the voice model answers from that. |
| Approval needed | Announced to the voice model ("the agent is waiting for approval to run `…`, the user must approve on screen"); **resolved only in the UI**. Approvals never come from audio in v1: a mis-heard "yes" must not run `bash`. |

A later milestone (M6) can add OpenCode-style control tools if the realtime
protocol supports client function tools, still never letting audio grant a
permission without an on-screen confirmation.

## 4. Components

### 4.1 `nexus/live/` (daemon-side manager, new)

| File | Owns |
| --- | --- |
| `live/protocol.py` | Frozen msgspec structs for the call payload (model, voice, instructions, delegation settings), the sideband event union (`session.created/updated`, input/output transcript delta+done, `turn.done`, `delegation.created`, `error`, `Unknown(raw_type)`), `context.append` messages, and `chunk_utf8(text, 500)` (char-safe, ≤ 500 UTF-8 bytes). Unknown event types parse to `Unknown`, never raise. |
| `live/transport.py` | `CodexLiveTransport`: POST SDP offer + payload to the signaling endpoint with `CodexOAuthManager.headers()` → SDP answer + call id; open the sideband WebSocket; serialised outbound queue; `session.close` on stop. Endpoints/headers pinned with a citation to both reference commits. 401 → "reconnect ChatGPT (`nexus auth codex login`)"; 403 → "voice not available on this plan or allowance used up". All errors through `redact_secrets`. |
| `live/attestation.py` | Only if the endpoint requires it (oh-my-pi sends Codex attestation when available). Spike decides; default is to omit. |
| `live/bridge.py` | `LiveBridge`: delegation → enqueue; subscribes to the session bus; turns `tool.requested` bursts into short progress commentary (bounded, rate-limited: ≤ 1 per 5 s), `turn.completed` → last visible assistant text → `"Agent Final Message:"` append on the delegation; `turn.failed/cancelled` → a failure append; `permission.requested` / `question.requested` → an announcement append. |
| `live/manager.py` | `LiveManager`: at most **one call per daemon** (like oh-my-pi's RPC rule; one speaker, one mic); state machine `idle → connecting → active ⇄ muted → closing → idle`; call deadline (`live.max_minutes`, default 30); idle-hangup; guarantees exactly one `live.ended` on every exit path (startup failure, explicit stop, transport error, session deleted, daemon shutdown, client detach timeout). |
| `live/prompts/instructions.md` | Bundled voice instructions (oh-my-pi style): delegate anything needing the repo, tools or verification; treat `commentary` as silent progress; never claim completion before the final message; approvals happen on screen; keep spoken answers short. Rendered with the workspace name and agent name, **not** the OS username (no personal data by default). Overridable at `<workspace>/.agents/live/instructions.md`. |
| `live/NOTICE` | Protocol provenance and the "undocumented API" caveat. |

The manager owns the sideband WebSocket. The WebRTC data channel in the helper
duplicates events; the helper forwards nothing from it except errors (oh-my-pi
does the same once sideband is open).

Lazy imports: a WebSocket client (`websockets` or `httpx-ws`) is added to an
optional `live` extra, not the base install. Decide in the spike; prefer one
already in the lockfile.

### 4.2 `nexus-live` helper (Rust, new crate `rust/live`)

A small stdio JSONL process. **No credentials, no network except the WebRTC
peer it is told about.**

- `webrtc` (webrtc-rs) peer: one Opus audio track + `oai-events` data channel.
- `cpal` capture (resample to 48 kHz for Opus) and playback; `opus` encode/decode,
  20 ms frames.
- Bounded input ring (≈ 500 ms); excess audio is dropped, counted, reported.
- Levels: input/output RMS, emitted ≤ 10 Hz.
- Echo handling, two stages:
  - v1: half-duplex gate while the speaker is active plus an RMS barge-in
    threshold (oh-my-pi's approach). Docs recommend headphones.
  - v2: `webrtc-audio-processing` (AEC3 + NS + AGC), or on macOS the
    VoiceProcessingIO audio unit. Optional ducking of other apps is **out of scope**.
- stdin commands: `offer` → stdout `{"type":"offer","sdp":…}`; `answer {sdp}`;
  `mute {on}`; `stop`. stdout events: `ready`, `levels`, `phase`
  (`listening|speaking`), `dc_error`, `dropped {frames}`, `closed {reason}`.
- Exits on stdin EOF (client crash ⇒ mic closes). Idempotent stop.
- Shipped as a second `[[tool.setuptools-rust.bins]]`; located like
  `ui/ratatui/run.py:binary_path` (`NEXUS_LIVE_BINARY` override). Building it
  must not become mandatory for wheels on platforms without audio: gate with a
  cargo feature and report `unsupported` in `LiveStatus` when absent.
- The desktop client may later link the crate as a library instead of spawning.

Why the helper lives on the **client** side, not in the daemon: microphone
permission on macOS attaches to the terminal / app that runs it (dictation
already captures in the client); a detached daemon may have no TCC attribution.
`/speak` playing from the daemon is fine; capture is not. Verify in the spike.

### 4.3 Host contract (`host/protocol.py`, `host/facade.py`)

| Command | Result | Notes |
| --- | --- | --- |
| `LiveStatus` | `available`, `reason` (`unsupported`/`no_credential`/`busy`/`disabled`), `active_session`, `phase`, `voice`, `muted` | No secrets; `busy` names the session that owns the call. |
| `LiveStart(session, offer_sdp, voice="", new_conversation=False)` | `LiveStartResult(call_id, answer_sdp)` | Daemon signals with OpenAI, opens sideband, returns only the SDP answer. Bounds: SDP ≤ 64 KiB. |
| `LiveMute(session, muted: bool \| None)` | `LiveState` | `None` toggles. The helper does the actual muting; the daemon records it so every attached view agrees. |
| `LiveStop(session, reason="user")` | `LiveState` | Idempotent. |
| `LiveHelperEvent(session, kind, detail)` | ack | Client forwards helper `closed`/`dc_error`/`dropped` so the daemon can end the call and log it. Bounded `detail`. |

Every client that supports Live calls these through `client/protocol.py`.
Protocol version bump only if an existing struct changes (it should not).

### 4.4 Events and view

New `live` group in `events.py`, folded by `view/reduce.py` into a
`LiveCallView` on `ConversationView.live` (and per-turn rows):

| Event | Durable | Data |
| --- | --- | --- |
| `live.started` | yes | `call_id`, `model` (`gpt-live-1-codex`), `voice`, `instructions_source` (`bundled`/path) and the rendered instructions text (bounded; it is context the voice model sees, so it is shown) |
| `live.transcript` | yes, **final only** | `role` (`user`/`assistant`), `text` (≤ `MAX_TEXT`), `turn` index |
| `live.transcript.delta` | bus-only if the sink supports non-durable fan-out; else coalesced ≤ 4 Hz | streaming caption |
| `live.delegated` | yes | `delegation_id`, `text`, `mode` (queue/steer), `queued_id` → links to the user message it created |
| `live.appended` | yes | `delegation_id`, `channel` (`commentary`/`final`/`notice`), `text` exactly as sent to the voice model |
| `live.phase` | bus-only | `connecting/listening/speaking/working/muted` |
| `live.levels` | bus-only, ≤ 10 Hz, newest wins | `in`, `out` RMS |
| `live.ended` | yes, exactly one per `live.started` | `reason`, `duration_s`, `error` (redacted) |

The user message a delegation creates carries `origin: {"kind": "live",
"delegation_id": …}` so every client labels it "via voice" and links it to the
spoken transcript. Replay rebuilds the call transcript, delegations and appends
from the log alone; levels and phase are cosmetic and need no replay.

Check during M1 whether `EventSink` already has a non-durable path (deltas
today go through `emitter.emit("text.delta", …)` in `core/loop.py`); if not,
add one in the session layer rather than persisting 10 Hz level frames.

### 4.5 Conversation continuity

Like OpenCode, one voice conversation per Nexus session: on the next
`/live`, the last N final transcript turns (default 12, ≤ 8 KiB) are sent in
the call instructions as "Earlier in this voice conversation". `/live new`
(OpenCode's `/voice-new`) starts without them. The carried-over text is shown
in the `live.started` row, so the user sees exactly what the voice model
starts with.

### 4.6 Config (`[live]` in `nexus.toml`, documented in `docs/config.md`)

```toml
[live]
enabled = true              # NEXUS_LIVE=off disables
voice = "default"           # passed to the call payload; list validated in the spike
max_minutes = 30            # hard call deadline
carry_turns = 12            # 0 disables continuity
progress = true             # send tool-progress commentary to the voice model
instructions = ""           # path override; empty = bundled / .agents/live/instructions.md
log_raw = false             # dev only: bounded raw sideband log under ~/.nexus/logs/live/
```

No separate `voiceModel`: there is no second agent in v1. The model id
`gpt-live-1-codex` is a pinned constant, not user config, until the protocol is
documented.

## 5. Client UX

### Ratatui (primary)

- `/live` / `Ctrl+X L` toggles the call; `/live mute` (also `Ctrl+X ;`? pick a
  free key), `/live new`, `/live status`. Start/stop cues reuse `play_cue`.
- While active, the composer area shows a **call bar** instead of hiding the
  editor (oh-my-pi replaces the editor; Nexus keeps typing possible, since
  typing during a call is ordinary steering): `● Live · gpt-live-1-codex ·
  listening · 02:41` + input/output level meters + `Muted` badge.
- Spoken turns render in the timeline as a labelled **Live** card: user and
  assistant captions, the delegation row ("Sent to agent: …", with mode), and
  each append ("Told voice: …", channel tag). Commentary is collapsed by
  default with a count; nothing is dropped.
- Errors (401/403/helper missing/mic denied) show the host's message plus the
  fix (`nexus auth codex login`, macOS Privacy → Microphone for the terminal).
- Bridge work goes in a new `ui/ratatui/live.py` (rule 3), helper process
  management in `ui_support/live_helper.py` (stdlib only).

### Desktop (GPUI)

Same commands and timeline card via the existing wire projection; call bar in
the composer chrome. The `.app` bundle needs `NSMicrophoneUsageDescription`.
Spawn the helper first; linking the crate is a later optimisation.

### CLI

`nexus live [--session ID] [--voice V]`: headless call in the terminal for
testing; prints captions and delegations. Useful for the live smoke test.

### Web

None (deprecated). `LiveCallView` events must not break it: the web reducer
already tolerates unknown events as diagnostics; verify the Playwright check
still passes.

## 6. Failure and cleanup

All paths converge on `LiveManager._end(reason)`, guarded so it runs once:

1. Stop accepting delegations; do **not** cancel the running agent turn (the
   work the user asked for continues; the transcript shows the call ended).
2. Send `session.close` on sideband if open (≤ 1 s), close WebSocket.
3. Tell attached clients `live.ended`; clients send `stop` to the helper and
   close stdin; the helper closes mic/speaker and the peer.
4. Persist `live.ended` with reason and redacted error.

Specific cases: signaling 401/403/5xx (no retry for 4xx; one retry for 5xx
before media); sideband drop mid-call (one reconnect attempt within 5 s, else
end); helper exit; client detach (no view holds the call for 30 s ⇒ end);
session deleted/archived; daemon shutdown; call deadline. Startup failure after
the helper produced an offer still sends `stop` to the helper.

## 7. Security review items

- OAuth bearer and attestation only in `live/transport.py`; never in events,
  logs, `LiveStatus`, helper stdin, or error text (`redact_secrets` + a
  regression test that asserts no `Bearer`/token substring reaches the log).
- The voice model's delegations are **untrusted input** exactly like a typed
  prompt; they go through the normal permission engine. Unattended approval
  policy stays whatever the session has; Live never auto-approves.
- Appends to the voice model contain the agent's final text, which may include
  file contents. That is the user's own session going to the same provider
  account the user signed in with; state it in `docs/security.md` and in the
  first-use consent dialog ("audio and summaries of the agent's work are sent
  to OpenAI via your ChatGPT account").
- First use is consent-gated (like voice download), stored once per machine.
- Outbound: signaling and sideband are fixed OpenAI hosts, not user URLs; they
  bypass `net/outbound.py` the way provider calls do (documented). WebRTC ICE
  goes from the helper directly to OpenAI's media servers.
- No raw audio is written anywhere. `log_raw` (dev only) logs sideband JSON,
  bounded and redacted.

## 8. Milestones

Each milestone ends with tests green, docs updated, and a Conventional Commit.

**M0 — Protocol spike (throwaway, `scripts/live_spike/`)**
- Reproduce signaling + sideband from both references with the real Codex
  credential; record which headers are required (attestation? `OpenAI-Beta`?),
  the voice list, and the exact event names.
- Prove the Rust peer connects, plays the greeting, and hears the mic.
- Verify macOS mic permission from a client-spawned helper vs a daemon-spawned one.
- Capture **redacted** fixtures (no SDP fingerprints, no tokens, no audio) into
  `tests/fixtures/live/` for the offline suite.
- Exit: a written note in this file (§10) with findings; go/no-go.

**M1 — Daemon core, no audio**
- `nexus/live/protocol.py`, `transport.py` (behind a `Transport` protocol),
  `manager.py`, events + reducer + `LiveCallView`, host commands, `client`
  methods.
- Tests with a `FakeLiveTransport` scripted from fixtures:
  `tests/test_live_protocol.py` (parsing, unknown events, 500-byte chunking on
  multibyte text), `test_live_manager.py` (state machine, exactly-one
  `live.ended`, deadline, busy), `test_live_view.py` (replay equals live state),
  `test_host_facade.py` additions, a security regression for redaction.

**M2 — Delegation bridge**
- `live/bridge.py` with `ScriptedProvider` turns: delegation idle → queue;
  during a turn → steer; final append text; failure/cancel append; permission
  announcement; progress rate limit; `origin` on the user message.
- Test: a delegation produces a normal turn whose transcript is identical to a
  typed prompt except the origin label.

**M3 — `nexus-live` helper**
- `rust/live` crate, JSONL protocol, bounded ring, levels, gate-based echo
  handling, idempotent stop, EOF exit. Cargo tests with a loopback peer and a
  fake audio device. Wheel packaging + `binary_path` lookup + `LiveStatus`
  `unsupported` when missing. CI builds it in the existing wheel matrix.

**M4 — Ratatui client** (implemented on `feat/gpt-live`; see `docs/live.md`; the `live-call` mock scenario was replaced by a fake transport and fake helper; `Ctrl+X L` was taken, so the key is `Ctrl+X D`)
- `/live` commands, `Ctrl+X L`, call bar, Live timeline card, consent dialog,
  error states. `ui_support/live_helper.py` with a fake helper for tests;
  `tests/test_ratatui_live.py`; native PTY check of the call bar with a fake
  helper and fake transport (dev-mode mock scenario `live-call`).

**M5 — Desktop + CLI**
- Wire projection fields, call bar, timeline card, mic entitlement;
  `nexus live` headless. Screenshot check via the native-app-review skill.

**M6 — Polish (optional, each separately)**
- AEC/NS via `webrtc-audio-processing`; spoken stop with on-screen
  confirmation; OpenCode-style control tools if the protocol supports client
  tools; desktop links the crate directly; per-call log export.

**Live verification** (marked `live`, deselected by default): one real call
start → spoken "what is in README" → delegation → final append → hang-up.
Record results with date; everything unrun stays "not verified".

## 9. Docs to update (same commits)

- New `docs/live.md` (architecture, flow, config, caveats) and a row in
  `docs/README.md`, `AGENTS.md` table, `docs/module-map.md` (every new module).
- `docs/host.md` (Live command group), `docs/events-and-view.md` (live group),
  `docs/config.md` (`[live]`, `NEXUS_LIVE`, `NEXUS_LIVE_BINARY`),
  `docs/security.md` (credential boundary, data sent, consent),
  `docs/surfaces.md` + `docs/ratatui-parity.md` + `docs/desktop.md` (UI),
  `docs/cli.md` (`/live`, `nexus live`), `docs/release.md` (second Rust bin),
  `docs/decisions.md` (why delegation not a voice agent; why helper is
  client-side; why approvals are UI-only; protocol is undocumented).

## 10. Open questions (answer in M0)

1. Does signaling require Codex attestation, and can Nexus produce it without
   copying non-licensed code? If it is mandatory and not reproducible: no-go.
2. Exact sideband URL/auth and whether the data channel alone suffices (would
   remove the WebSocket dependency).
3. Supported voices and whether `voice` can change mid-call.
4. Does the realtime protocol accept client function tools (needed for M6)?
5. Plan/usage limits: is there a usage endpoint to show remaining voice
   allowance in `ProvidersUsage`?
6. Licensing: OpenCode plugin and oh-my-pi licenses; we re-implement from the
   observed protocol and cite commits, copying no code, as with `auth/codex.py`.
7. Where the helper runs for a remote/SSH session (client machine vs daemon
   machine): v1 assumes they are the same machine (loopback-only daemon).
